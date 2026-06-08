from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from integrations.krepo_adapter import (
    run_calls_skill,
    run_params_skill,
    run_report_skill,
    run_subsource_skill,
)


class KRepoSkillsTest(unittest.TestCase):
    def test_krepo_skills_write_to_axf_artifact_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "artifacts"
            inputs = {
                "repo": "/linux-7.0",
                "function": "can_send",
                "file": "net/can/af_can.c",
                "artifact_dir": str(artifact_dir),
                "max_depth": 1,
                "max_functions": 30,
                "call_depth": 3,
            }

            def fake_run(command, **_kwargs):
                text = "output for " + command[2] + "\n"
                return subprocess.CompletedProcess(command, 0, text, "")

            with mock.patch("integrations.krepo_adapter.subprocess.run", side_effect=fake_run):
                report = run_report_skill(inputs)
                params = run_params_skill(inputs)
                calls = run_calls_skill(inputs)
                subsource = run_subsource_skill(inputs)

            self.assertEqual(report["outputs"]["report_json"], str(artifact_dir / "krepo" / "report.json"))
            self.assertEqual(report["outputs"]["report_md"], str(artifact_dir / "krepo" / "report.md"))
            self.assertEqual(params["outputs"]["params_text"], str(artifact_dir / "krepo" / "params.txt"))
            self.assertEqual(calls["outputs"]["calls_text"], str(artifact_dir / "krepo" / "calls.txt"))
            self.assertEqual(subsource["outputs"]["subsource_c"], str(artifact_dir / "krepo" / "subsource.c"))
            self.assertTrue((artifact_dir / "krepo" / "params.txt").exists())


if __name__ == "__main__":
    unittest.main()
