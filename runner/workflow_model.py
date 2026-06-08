from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class StepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class LaneSpec:
    name: str
    max_concurrency: int = 1


@dataclass(frozen=True)
class StepSpec:
    id: str
    skill: str
    lane: str
    depends_on: tuple[str, ...] = ()
    inputs: dict[str, Any] = field(default_factory=dict)
    timeout: int | None = None
    retry: int = 0


@dataclass(frozen=True)
class WorkflowSpec:
    name: str
    path: Path | None = None
    description: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    lanes: dict[str, LaneSpec] = field(default_factory=dict)
    steps: tuple[StepSpec, ...] = ()

    def step_by_id(self) -> dict[str, StepSpec]:
        return {step.id: step for step in self.steps}


@dataclass
class StepResult:
    status: StepStatus = StepStatus.SUCCESS
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    logs: str = ""
    returncode: int = 0


@dataclass
class StepState:
    id: str
    skill: str
    lane: str
    status: StepStatus = StepStatus.PENDING
    attempt: int = 0
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "skill": self.skill,
            "lane": self.lane,
            "status": self.status.value,
            "attempt": self.attempt,
            "outputs": self.outputs,
            "error": self.error,
        }


@dataclass
class RunState:
    workflow: str
    steps: dict[str, StepState]
    status: StepStatus = StepStatus.PENDING

    def to_json(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "status": self.status.value,
            "steps": {step_id: state.to_json() for step_id, state in self.steps.items()},
        }
