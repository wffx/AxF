from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .state_store import StateStore
from .workflow_model import RunState, StepResult, StepSpec, StepState, StepStatus, WorkflowSpec


StepExecutor = Callable[[StepSpec, dict], StepResult]


@dataclass
class SchedulerContext:
    inputs: dict
    run_dir: Path | None = None
    steps: dict | None = None


class LaneScheduler:
    def __init__(
        self,
        workflow: WorkflowSpec,
        executor: StepExecutor,
        *,
        state_store: StateStore | None = None,
        fail_fast: bool = True,
    ):
        self.workflow = workflow
        self.executor = executor
        self.state_store = state_store
        self.fail_fast = fail_fast

    def run(
        self,
        inputs: dict,
        *,
        run_dir: Path | None = None,
        state: RunState | None = None,
    ) -> RunState:
        if self.state_store and (run_dir is None or state is None):
            run_dir, state = self.state_store.create_run(self.workflow, inputs)
        if state is None:
            state = RunState(
                workflow=self.workflow.name,
                steps={
                    step.id: StepState(id=step.id, skill=step.skill, lane=step.lane)
                    for step in self.workflow.steps
                },
            )
        pending = {step.id: step for step in self.workflow.steps}
        running: dict[Future[StepResult], StepSpec] = {}
        lane_running = {lane: 0 for lane in self.workflow.lanes}
        max_workers = max(1, sum(lane.max_concurrency for lane in self.workflow.lanes.values()))
        context = {"inputs": inputs, "run_dir": run_dir, "steps": {}}
        failed = False

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            while pending or running:
                submitted = False
                if not failed:
                    for step in list(pending.values()):
                        if not self._dependencies_satisfied(step, state):
                            continue
                        lane = self.workflow.lanes[step.lane]
                        if lane_running[step.lane] >= lane.max_concurrency:
                            continue
                        del pending[step.id]
                        lane_running[step.lane] += 1
                        if self.state_store and run_dir:
                            self.state_store.step_started(run_dir, state, step, state.steps[step.id].attempt + 1)
                        else:
                            state.steps[step.id].status = StepStatus.RUNNING
                            state.steps[step.id].attempt += 1
                        running[pool.submit(self.executor, step, context)] = step
                        submitted = True
                if failed and pending:
                    for step in pending.values():
                        state.steps[step.id].status = StepStatus.SKIPPED
                        state.steps[step.id].error = "skipped after workflow failure"
                    pending.clear()
                    if not running:
                        break
                if not running:
                    if pending and not submitted:
                        blocked = next(iter(pending))
                        state.steps[blocked].status = StepStatus.SKIPPED
                        state.steps[blocked].error = "dependencies were not satisfied"
                        del pending[blocked]
                        continue
                    break
                done, _ = wait(running.keys(), return_when=FIRST_COMPLETED)
                for future in done:
                    step = running.pop(future)
                    lane_running[step.lane] -= 1
                    result = self._future_result(future)
                    state.steps[step.id].status = result.status
                    state.steps[step.id].outputs = result.outputs
                    state.steps[step.id].error = result.error
                    context["steps"][step.id] = result.outputs
                    if self.state_store and run_dir:
                        self.state_store.step_completed(run_dir, state, step, result)
                    if result.status != StepStatus.SUCCESS and self.fail_fast:
                        failed = True

        final_status = StepStatus.FAILED if any(
            item.status in {StepStatus.FAILED, StepStatus.SKIPPED} and item.error
            for item in state.steps.values()
        ) else StepStatus.SUCCESS
        state.status = final_status
        if self.state_store and run_dir:
            self.state_store.finalize(run_dir, state, final_status)
        return state

    def _dependencies_satisfied(self, step: StepSpec, state: RunState) -> bool:
        return all(state.steps[dep].status == StepStatus.SUCCESS for dep in step.depends_on)

    def _future_result(self, future: Future[StepResult]) -> StepResult:
        try:
            result = future.result()
        except Exception as exc:
            return StepResult(status=StepStatus.FAILED, error=str(exc), returncode=1)
        if not isinstance(result, StepResult):
            return StepResult(status=StepStatus.FAILED, error="executor returned non-StepResult", returncode=1)
        return result
