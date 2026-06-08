from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from .skill_loader import load_skill
from .workflow_model import StepResult, StepSpec, StepStatus


def execute_python_skill(step: StepSpec, context: dict[str, Any]) -> StepResult:
    skill = load_skill(step.skill)
    if skill.executor != "python":
        return StepResult(status=StepStatus.FAILED, error=f"unsupported executor: {skill.executor}")
    if ":" not in skill.entrypoint:
        return StepResult(status=StepStatus.FAILED, error=f"invalid entrypoint: {skill.entrypoint}")
    module_name, func_name = skill.entrypoint.split(":", 1)
    module = importlib.import_module(module_name)
    func = getattr(module, func_name)
    inputs = dict(context.get("inputs") or {})
    inputs.update(step.inputs)
    run_dir = context.get("run_dir")
    if run_dir and "artifact_dir" not in inputs:
        inputs["artifact_dir"] = str(run_dir / "artifacts")
        inputs["run_dir"] = str(run_dir)
    if context.get("steps") is not None:
        inputs["steps"] = context.get("steps")
    result = func(inputs)
    if not isinstance(result, dict):
        return StepResult(status=StepStatus.FAILED, error="skill returned non-object result")
    outputs = dict(result.get("outputs") or result)
    if run_dir:
        outputs = _relativize_outputs(outputs, Path(run_dir))
    return StepResult(
        status=StepStatus.SUCCESS,
        outputs=outputs,
        logs=_logs_from_result(result),
    )


def _logs_from_result(result: dict[str, Any]) -> str:
    lines: list[str] = []
    provider = result.get("provider")
    if provider:
        lines.append(f"provider: {provider}")
    for command in result.get("commands") or []:
        lines.append("$ " + " ".join(str(part) for part in command))
    return "\n".join(lines) + ("\n" if lines else "")


def _relativize_outputs(outputs: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in outputs.items():
        if isinstance(value, str):
            normalized[key] = _relative_path_text(value, run_dir)
        elif isinstance(value, list):
            normalized[key] = [
                _relative_path_text(item, run_dir) if isinstance(item, str) else item
                for item in value
            ]
        elif isinstance(value, dict):
            normalized[key] = _relativize_outputs(value, run_dir)
        else:
            normalized[key] = value
    return normalized


def _relative_path_text(value: str, run_dir: Path) -> str:
    try:
        path = Path(value)
    except (TypeError, ValueError):
        return value
    if not path.is_absolute():
        return value
    try:
        return str(path.relative_to(run_dir))
    except ValueError:
        return value
