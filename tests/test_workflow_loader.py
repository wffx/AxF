from __future__ import annotations

import unittest

from runner.errors import WorkflowConfigError
from runner.workflow_loader import load_workflow, workflow_from_dict


class WorkflowLoaderTest(unittest.TestCase):
    def test_loads_builtin_fuzzing_pipeline_with_krepo_lane(self) -> None:
        workflow = load_workflow("fuzzing-pipeline.yaml")

        self.assertEqual(workflow.name, "fuzzing-pipeline")
        self.assertIn("krepo", workflow.lanes)
        self.assertEqual(workflow.lanes["krepo"].max_concurrency, 3)
        self.assertIn("krepo-report", {step.skill for step in workflow.steps})
        self.assertIn("krepo-subsource", {step.skill for step in workflow.steps})

    def test_rejects_unknown_skill(self) -> None:
        with self.assertRaisesRegex(WorkflowConfigError, "skill not found"):
            workflow_from_dict(
                {
                    "name": "bad",
                    "lanes": {"control": {"max_concurrency": 1}},
                    "steps": [{"id": "missing", "skill": "does-not-exist", "lane": "control"}],
                }
            )

    def test_rejects_unknown_dependency(self) -> None:
        with self.assertRaisesRegex(WorkflowConfigError, "unknown dependency"):
            workflow_from_dict(
                {
                    "name": "bad",
                    "lanes": {"control": {"max_concurrency": 1}},
                    "steps": [
                        {
                            "id": "preflight",
                            "skill": "preflight",
                            "lane": "control",
                            "depends_on": ["missing"],
                        }
                    ],
                }
            )


if __name__ == "__main__":
    unittest.main()
