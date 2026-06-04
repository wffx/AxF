from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from frontend.server import HARNESS_AGENT_ARTIFACT, PipelineStep, default_config
from frontend.terminal import (
    TerminalTaskRunner,
    config_from_args,
    docker_terminal_command,
    main,
    parse_args,
    parse_artifacts,
    prompt_api_key,
    should_launch_docker,
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

    def test_terminal_run_defaults_to_docker_outside_container(self) -> None:
        args = parse_args(["run"])

        with (
            mock.patch("frontend.terminal._running_in_docker", return_value=False),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            launch = should_launch_docker(args)

        self.assertTrue(launch)

    def test_terminal_run_local_disables_docker_launcher(self) -> None:
        args = parse_args(["run", "--local"])

        with (
            mock.patch("frontend.terminal._running_in_docker", return_value=False),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            launch = should_launch_docker(args)

        self.assertFalse(launch)

    def test_terminal_runtime_env_can_disable_docker_launcher(self) -> None:
        args = parse_args(["run"])

        with (
            mock.patch("frontend.terminal._running_in_docker", return_value=False),
            mock.patch.dict(os.environ, {"AXF_TERMINAL_RUNTIME": "local"}, clear=True),
        ):
            launch = should_launch_docker(args)

        self.assertFalse(launch)

    def test_docker_terminal_command_wraps_original_args(self) -> None:
        with mock.patch.dict(os.environ, {"AXF_COMPOSE_COMMAND": "docker compose"}, clear=True):
            command = docker_terminal_command(["run", "--non-interactive"])

        self.assertEqual(command[:2], ["docker", "compose"])
        self.assertIn("dev", command)
        self.assertEqual(command[-5:], ["python", "-m", "frontend.terminal", "run", "--non-interactive"])

    def test_docker_terminal_command_falls_back_to_legacy_compose(self) -> None:
        def fake_run(command: list[str], **_kwargs: object) -> mock.Mock:
            if command == ["docker", "compose", "version"]:
                return mock.Mock(returncode=1)
            if command == ["docker-compose", "version"]:
                return mock.Mock(returncode=0)
            return mock.Mock(returncode=1)

        with (
            mock.patch.dict(os.environ, {"AXF_DOCKER_CONTEXT": ""}, clear=True),
            mock.patch("frontend.terminal.subprocess.run", side_effect=fake_run),
        ):
            command = docker_terminal_command(["run"])

        self.assertEqual(command[0], "docker-compose")
        self.assertEqual(command[1], "-f")

    def test_docker_terminal_command_can_use_configured_compose_command(self) -> None:
        with mock.patch.dict(os.environ, {"AXF_COMPOSE_COMMAND": "docker-compose"}, clear=True):
            command = docker_terminal_command(["run"])

        self.assertEqual(command[:2], ["docker-compose", "-f"])

    def test_main_uses_docker_launcher_before_config_validation(self) -> None:
        with (
            mock.patch("frontend.terminal.should_launch_docker", return_value=True),
            mock.patch("frontend.terminal.run_in_docker", return_value=23) as launcher,
        ):
            returncode = main(["run", "--non-interactive"])

        self.assertEqual(returncode, 23)
        launcher.assert_called_once_with(["run", "--non-interactive"])

    def test_non_interactive_api_key_sets_environment_without_config(self) -> None:
        args = parse_args(
            [
                "run",
                "--non-interactive",
                "--repo",
                "../linux-7.0",
                "--function",
                "can_send",
                "--artifacts",
                "harness_generation_agent",
                "--llm-mode",
                "api",
                "--model",
                "glm-5.1",
                "--model-url",
                "https://example.invalid/v1/chat/completions",
                "--api-key-env",
                "AXF_TEST_API_KEY",
                "--api-key",
                "sk-secret",
            ]
        )

        with mock.patch.dict(os.environ, {}, clear=True):
            config = config_from_args(args, output=io.StringIO())
            env_value = os.environ["AXF_TEST_API_KEY"]

        self.assertEqual(config["chat_url"], "https://example.invalid/v1/chat/completions")
        self.assertEqual(env_value, "sk-secret")
        self.assertNotIn("api_key", config)

    def test_prompt_api_key_sets_environment(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            prompt_api_key("AXF_PROMPT_API_KEY", None, lambda _prompt: "sk-prompt", io.StringIO())
            env_value = os.environ["AXF_PROMPT_API_KEY"]

        self.assertEqual(env_value, "sk-prompt")

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
