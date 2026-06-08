from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .workflow_model import RunState, StepResult, StepSpec, StepState, StepStatus, WorkflowSpec


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_ROOT = PROJECT_ROOT / "state" / "runs"


class StateStore:
    def __init__(self, root: Path | None = None):
        self.root = root or DEFAULT_STATE_ROOT
        self.root.mkdir(parents=True, exist_ok=True)

    def create_run(
        self,
        workflow: WorkflowSpec,
        inputs: dict[str, Any],
        *,
        run_id: str | None = None,
    ) -> tuple[Path, RunState]:
        run_id = run_id or uuid.uuid4().hex[:12]
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "logs").mkdir()
        (run_dir / "prompts").mkdir()
        (run_dir / "artifacts").mkdir()
        if workflow.path and workflow.path.exists():
            (run_dir / "workflow.yaml").write_text(workflow.path.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            (run_dir / "workflow.yaml").write_text(workflow.name + "\n", encoding="utf-8")
        (run_dir / "inputs.json").write_text(json.dumps(inputs, ensure_ascii=False, indent=2), encoding="utf-8")
        state = RunState(
            workflow=workflow.name,
            steps={
                step.id: StepState(id=step.id, skill=step.skill, lane=step.lane)
                for step in workflow.steps
            },
        )
        self.write_state(run_dir, state)
        self.emit(run_dir, "init", f"workflow started: {workflow.name}")
        return run_dir, state

    def artifact_dir(self, run_dir: Path) -> Path:
        path = run_dir / "artifacts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def log_path(self, run_dir: Path, step_id: str) -> Path:
        path = run_dir / "logs" / f"{step_id}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_state(self, run_dir: Path, state: RunState) -> None:
        (run_dir / "state.json").write_text(
            json.dumps(state.to_json(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def emit(self, run_dir: Path, phase: str, message: str, **extra: Any) -> None:
        event = {"ts": time.time(), "phase": phase, "message": message}
        event.update(extra)
        with (run_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def step_started(self, run_dir: Path, state: RunState, step: StepSpec, attempt: int) -> None:
        step_state = state.steps[step.id]
        step_state.status = StepStatus.RUNNING
        step_state.attempt = attempt
        self.write_state(run_dir, state)
        self.emit(run_dir, step.id, f"step started: {step.id}", skill=step.skill, lane=step.lane, attempt=attempt)

    def step_completed(self, run_dir: Path, state: RunState, step: StepSpec, result: StepResult) -> None:
        step_state = state.steps[step.id]
        step_state.status = result.status
        step_state.outputs = result.outputs
        step_state.error = result.error
        if result.logs:
            self.log_path(run_dir, step.id).write_text(result.logs, encoding="utf-8")
        self.write_state(run_dir, state)
        self.emit(
            run_dir,
            step.id,
            f"step finished: {step.id} -> {result.status.value}",
            skill=step.skill,
            lane=step.lane,
            status=result.status.value,
        )

    def finalize(self, run_dir: Path, state: RunState, status: StepStatus) -> None:
        state.status = status
        self.write_state(run_dir, state)
        self.emit(run_dir, "complete", f"workflow finished: {status.value}", status=status.value)
