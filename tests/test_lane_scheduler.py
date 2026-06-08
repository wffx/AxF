from __future__ import annotations

import threading
import time
import unittest

from runner.lane_scheduler import LaneScheduler
from runner.workflow_model import LaneSpec, StepResult, StepSpec, StepStatus, WorkflowSpec


class LaneSchedulerTest(unittest.TestCase):
    def test_lane_concurrency_limit_is_enforced(self) -> None:
        workflow = WorkflowSpec(
            name="concurrency",
            lanes={"krepo": LaneSpec("krepo", max_concurrency=2)},
            steps=tuple(
                StepSpec(id=f"s{index}", skill="krepo-report", lane="krepo")
                for index in range(4)
            ),
        )
        lock = threading.Lock()
        active = 0
        max_active = 0

        def executor(_step, _context):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return StepResult(outputs={})

        state = LaneScheduler(workflow, executor).run({})

        self.assertEqual(state.status, StepStatus.SUCCESS)
        self.assertEqual(max_active, 2)

    def test_failed_step_skips_dependents(self) -> None:
        workflow = WorkflowSpec(
            name="failure",
            lanes={"control": LaneSpec("control", 1)},
            steps=(
                StepSpec(id="a", skill="preflight", lane="control"),
                StepSpec(id="b", skill="report-gen", lane="control", depends_on=("a",)),
            ),
        )

        def executor(step, _context):
            if step.id == "a":
                return StepResult(status=StepStatus.FAILED, error="boom")
            return StepResult()

        state = LaneScheduler(workflow, executor).run({})

        self.assertEqual(state.status, StepStatus.FAILED)
        self.assertEqual(state.steps["a"].status, StepStatus.FAILED)
        self.assertEqual(state.steps["b"].status, StepStatus.SKIPPED)


if __name__ == "__main__":
    unittest.main()
