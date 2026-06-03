from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from frontend.server import HARNESS_AGENT_ARTIFACT, PipelineStep, default_config
from frontend.terminal import (
    TerminalTaskRunner,
    config_from_args,
    parse_args,
    parse_artifacts,
    terminal_default_config,
)


class TerminalTest(unittest.TestCase):
    def test_parse_artifacts_accepts_comma_separated_ids(self) -> None:
        artifacts = parse_artifacts("report_json,params,harness_generation_agent")

        self.assertEqual(artifacts, ["report_json", "params", HARNESS_AGENT_ARTIFACT])

    def test_parse_artifacts_accepts_menu_numbers(self) -> None:
        artifacts = parse_artifacts("2,6,7")

        self.assertEqual(artifacts, ["report_json", "params", HARNESS_AGENT_ARTIFACT])

    def test_non_interactive_requires_core_fields(self) -> None:
        args = parse_args(["run", "--non-interactive", "--repo", "../linux-7.0"])

        with self.assertRaisesRegex(ValueError, "function"):
            config_from_args(args, output=io.StringIO())

    def test_non_interactive_config_uses_selected_artifacts_only(self) -> None:
        args = parse_args(
            [
                "run",
                "--non-interactive",
                "--repo",
                "../linux-7.0",
                "--function",
                "can_send",
                "--file",
                "net/can/af_can.c",
                "--artifacts",
                "report_json,params,harness_generation_agent",
                "--llm-mode",
                "opencode",
                "--opencode-tool",
                "nga",
                "--opencode-executable",
                "nga",
                "--opencode-model",
                "anthropic/claude-sonnet-4",
                "--model",
                "glm-5.1",
                "--clang-mode",
                "wsl",
                "--knowledge-dir",
                "workspace/web/tasks/old",
            ]
        )

        config = config_from_args(args, output=io.StringIO())

        self.assertEqual(config["artifacts"], ["report_json", "params", HARNESS_AGENT_ARTIFACT])
        self.assertEqual(config["llm_mode"], "opencode")
        self.assertEqual(config["opencode_tool"], "nga")
        self.assertEqual(config["opencode_executable"], "nga")
        self.assertEqual(config["opencode_model"], "anthropic/claude-sonnet-4")
        self.assertEqual(config["model"], "glm-5.1")
        self.assertEqual(config["clang_mode"], "wsl")
        self.assertEqual(config["file"], "net/can/af_can.c")
        self.assertEqual(config["knowledge_dir"], "workspace/web/tasks/old")

    def test_terminal_default_config_matches_frontend_defaults(self) -> None:
        self.assertEqual(terminal_default_config(), default_config())

    def test_runner_writes_task_files_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "task"
            output = io.StringIO()
            runner = TerminalTaskRunner(Path(tmp), output)
            step = PipelineStep(
                "fake",
                [sys.executable, "-c", "print('artifact output')"],
                "fake",
                task_dir / "fake.txt",
            )

            returncode = runner.run_steps("task", task_dir, {"function": "fake_fn"}, [step])

            task_json = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
            events = (task_dir / "events.jsonl").read_text(encoding="utf-8")
            fake_output = (task_dir / "fake.txt").read_text(encoding="utf-8")

        self.assertEqual(returncode, 0)
        self.assertEqual(task_json["id"], "task")
        self.assertIn("fake_fn", task_json["config"]["function"])
        self.assertIn("artifact output", fake_output)
        self.assertIn("任务已开始", events)
        self.assertIn("任务完成", events)
        self.assertIn("产物目录", output.getvalue())

    def test_runner_returns_nonzero_for_failed_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "task"
            output = io.StringIO()
            runner = TerminalTaskRunner(Path(tmp), output)
            step = PipelineStep(
                "fake",
                [sys.executable, "-c", "import sys; print('bad step'); sys.exit(3)"],
                "fake",
                task_dir / "fake.txt",
                capture_stdout=False,
            )

            returncode = runner.run_steps("task", task_dir, {"function": "fake_fn"}, [step])
            log_text = (task_dir / "task.log").read_text(encoding="utf-8")
            events = (task_dir / "events.jsonl").read_text(encoding="utf-8")

        self.assertEqual(returncode, 3)
        self.assertIn("bad step", log_text)
        self.assertIn("退出码：3", events)
        self.assertIn("失败", output.getvalue())

    def test_runner_reuses_artifact_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "task"
            source = root / "old-report.json"
            source.write_text('{"ok": true}', encoding="utf-8")
            output = io.StringIO()
            runner = TerminalTaskRunner(root, output)
            step = PipelineStep(
                "report_json",
                ["reuse", str(source), str(task_dir / "report.json")],
                "report_json",
                task_dir / "report.json",
                capture_stdout=False,
                reuse_from=source,
            )

            returncode = runner.run_steps("task", task_dir, {"function": "can_send"}, [step])
            copied = (task_dir / "report.json").read_text(encoding="utf-8")
            events = (task_dir / "events.jsonl").read_text(encoding="utf-8")

        self.assertEqual(returncode, 0)
        self.assertIn('"ok": true', copied)
        self.assertIn("正在复用", events)

    def test_runner_stops_when_rg_preflight_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "task"
            output = io.StringIO()
            runner = TerminalTaskRunner(Path(tmp), output)
            step = PipelineStep(
                "fake",
                [sys.executable, "-c", "print('should not run')"],
                "fake",
                task_dir / "fake.txt",
            )

            with (
                mock.patch("frontend.terminal.ensure_rg_available", side_effect=RuntimeError("missing rg")),
                mock.patch("frontend.terminal.subprocess.Popen") as popen,
            ):
                returncode = runner.run_steps("task", task_dir, {"function": "fake_fn"}, [step])

            events = (task_dir / "events.jsonl").read_text(encoding="utf-8")

        self.assertEqual(returncode, 127)
        self.assertEqual(popen.call_count, 0)
        self.assertIn("missing rg", events)
        self.assertIn("错误：missing rg", output.getvalue())

    def test_async_submit_starts_worker_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            runner = TerminalTaskRunner(Path(tmp), output)
            config = {
                "repo": "./linux-7.0",
                "function": "can_send",
                "artifacts": ["report_json"],
            }

            with mock.patch("frontend.terminal.subprocess.Popen") as popen:
                returncode = runner.submit_async(config)

            task_dirs = list(Path(tmp).iterdir())
            config_text = (task_dirs[0] / "config.json").read_text(encoding="utf-8")

        self.assertEqual(returncode, 0)
        self.assertEqual(popen.call_count, 1)
        self.assertIn('"function": "can_send"', config_text)
        self.assertIn("已提交异步任务", output.getvalue())


if __name__ == "__main__":
    unittest.main()
