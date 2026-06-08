from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import SkillConfigError, WorkflowConfigError
from .simple_yaml import load_simple_yaml
from .skill_loader import DEFAULT_SKILLS_ROOT, load_skill
from .workflow_model import LaneSpec, StepSpec, WorkflowSpec


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKFLOWS_ROOT = PROJECT_ROOT / "workflows"


def load_workflow(path: str | Path, *, skills_root: Path | None = None) -> WorkflowSpec:
    workflow_path = Path(path)
    if not workflow_path.is_absolute():
        workflow_path = DEFAULT_WORKFLOWS_ROOT / workflow_path
    data = load_simple_yaml(workflow_path)
    workflow = workflow_from_dict(data, path=workflow_path, skills_root=skills_root)
    return workflow


def workflow_from_dict(
    data: dict[str, Any],
    *,
    path: Path | None = None,
    skills_root: Path | None = None,
) -> WorkflowSpec:
    name = str(data.get("name") or "").strip()
    if not name:
        raise WorkflowConfigError("workflow name is required")
    lanes_data = data.get("lanes") or {}
    if not isinstance(lanes_data, dict) or not lanes_data:
        raise WorkflowConfigError("workflow lanes are required")
    lanes: dict[str, LaneSpec] = {}
    for lane_name, lane_value in lanes_data.items():
        if not isinstance(lane_value, dict):
            raise WorkflowConfigError(f"lane must be a mapping: {lane_name}")
        max_concurrency = int(lane_value.get("max_concurrency") or 1)
        if max_concurrency < 1:
            raise WorkflowConfigError(f"lane max_concurrency must be >= 1: {lane_name}")
        lanes[str(lane_name)] = LaneSpec(str(lane_name), max_concurrency)

    steps_data = data.get("steps") or []
    if not isinstance(steps_data, list) or not steps_data:
        raise WorkflowConfigError("workflow steps are required")
    steps: list[StepSpec] = []
    seen: set[str] = set()
    root = skills_root or DEFAULT_SKILLS_ROOT
    for raw_step in steps_data:
        if not isinstance(raw_step, dict):
            raise WorkflowConfigError("workflow step must be a mapping")
        step_id = str(raw_step.get("id") or "").strip()
        if not step_id:
            raise WorkflowConfigError("step id is required")
        if step_id in seen:
            raise WorkflowConfigError(f"duplicate step id: {step_id}")
        seen.add(step_id)
        skill_name = str(raw_step.get("skill") or "").strip()
        if not skill_name:
            raise WorkflowConfigError(f"step {step_id} requires skill")
        try:
            skill = load_skill(skill_name, root)
        except SkillConfigError as exc:
            raise WorkflowConfigError(str(exc)) from exc
        lane = str(raw_step.get("lane") or skill.default_lane).strip()
        if lane not in lanes:
            raise WorkflowConfigError(f"step {step_id} uses unknown lane: {lane}")
        depends_on = raw_step.get("depends_on") or []
        if isinstance(depends_on, str):
            depends_on = [depends_on]
        if not isinstance(depends_on, list):
            raise WorkflowConfigError(f"step {step_id} depends_on must be a list")
        inputs = raw_step.get("inputs") or {}
        if not isinstance(inputs, dict):
            raise WorkflowConfigError(f"step {step_id} inputs must be a mapping")
        steps.append(
            StepSpec(
                id=step_id,
                skill=skill_name,
                lane=lane,
                depends_on=tuple(str(dep) for dep in depends_on),
                inputs=inputs,
                timeout=_optional_int(raw_step.get("timeout")),
                retry=int(raw_step.get("retry") or 0),
            )
        )
    known_steps = {step.id for step in steps}
    for step in steps:
        missing = [dep for dep in step.depends_on if dep not in known_steps]
        if missing:
            raise WorkflowConfigError(f"step {step.id} has unknown dependency: {missing[0]}")

    inputs = data.get("inputs") or {}
    if not isinstance(inputs, dict):
        raise WorkflowConfigError("workflow inputs must be a mapping")
    return WorkflowSpec(
        name=name,
        path=path,
        description=str(data.get("description") or ""),
        inputs=inputs,
        lanes=lanes,
        steps=tuple(steps),
    )


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)
