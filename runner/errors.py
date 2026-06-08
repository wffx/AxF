from __future__ import annotations


class WorkflowError(RuntimeError):
    pass


class WorkflowConfigError(WorkflowError):
    pass


class SkillConfigError(WorkflowError):
    pass


class StepExecutionError(WorkflowError):
    pass
