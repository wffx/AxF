from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from runner.executors import execute_python_skill
from runner.lane_scheduler import LaneScheduler
from runner.state_store import StateStore
from runner.workflow_model import LaneSpec, StepSpec, StepStatus, WorkflowSpec


class StateStoreTest(unittest.TestCase):
    def test_state_store_records_relative_outputs_and_events(self) -> None:
        workflow = WorkflowSpec(
            name="report-only",
            lanes={"report": LaneSpec("report", 1)},
            steps=(StepSpec(id="final_report", skill="report-gen", lane="report"),),
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            state = LaneScheduler(workflow, execute_python_skill, state_store=store).run(
                {"function": "can_send", "repo": "/linux-7.0"}
            )
            run_dirs = [path for path in Path(tmp).iterdir() if path.is_dir()]
            state_json = json.loads((run_dirs[0] / "state.json").read_text(encoding="utf-8"))
            events = (run_dirs[0] / "events.jsonl").read_text(encoding="utf-8")

        self.assertEqual(state.status, StepStatus.SUCCESS)
        self.assertEqual(state_json["steps"]["final_report"]["outputs"]["report_md"], "report.md")
        self.assertIn("workflow started", events)
        self.assertIn("workflow finished", events)


if __name__ == "__main__":
    unittest.main()
