from __future__ import annotations

import unittest

from runner.lane_scheduler import LaneScheduler
from runner.workflow_loader import load_workflow
from runner.workflow_model import StepResult, StepStatus


class FuzzingWorkflowTest(unittest.TestCase):
    def test_fuzzing_workflow_consumes_krepo_artifacts_before_harness(self) -> None:
        workflow = load_workflow("fuzzing-pipeline.yaml")

        def executor(step, context):
            if step.id == "preflight":
                return StepResult(outputs={"repo": "/linux-7.0"})
            if step.skill.startswith("krepo-"):
                return StepResult(outputs={step.id: f"artifacts/krepo/{step.id}.txt"})
            if step.id == "harness":
                self.assertIn("report", context["steps"])
                self.assertIn("params", context["steps"])
                self.assertIn("calls", context["steps"])
                self.assertIn("subsource", context["steps"])
                return StepResult(outputs={"harness_spec": "artifacts/harness/harness_spec.json"})
            return StepResult(outputs={"report_md": "report.md"})

        state = LaneScheduler(workflow, executor).run({"repo": "/linux-7.0", "function": "can_send"})

        self.assertEqual(state.status, StepStatus.SUCCESS)
        self.assertEqual(state.steps["harness"].outputs["harness_spec"], "artifacts/harness/harness_spec.json")


if __name__ == "__main__":
    unittest.main()
