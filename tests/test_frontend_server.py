from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from agents.harness_generation.agent import parse_model_json
from frontend.server import (
    PipelineStep,
    build_steps,
    default_config,
    _completion_message,
    _extra_artifacts_for_step,
    _harness_failure_message,
    _harness_failure_message_for_step,
    _harness_events_for_step,
    _harness_summary,
)


class FrontendServerTest(unittest.TestCase):
    def test_build_steps_creates_selected_knowledge_commands(self) -> None:
        steps = build_steps(
            {
                "repo": "./linux-7.0",
                "function": "can_send",
                "file": "net/can/af_can.c",
                "artifacts": ["report_json", "subsource", "calls", "params"],
                "max_depth": 1,
                "max_functions": 30,
                "call_depth": 3,
            },
            Path("/tmp/axf-task"),
        )

        self.assertEqual([step.name for step in steps], ["report_json", "subsource", "calls", "params"])
        commands = [" ".join(step.command) for step in steps]
        self.assertIn("cpp_meta_query.py report can_send", commands[0])
        self.assertIn("--format json", commands[0])
        self.assertIn("cpp_meta_query.py subsource can_send", commands[1])
        self.assertIn("--max-functions 30", commands[1])
        self.assertIn("cpp_meta_query.py calls can_send", commands[2])
        self.assertIn("--max-depth 3", commands[2])
        self.assertTrue(steps[1].artifact_path.name.endswith("_subsource_bundle.c"))

    def test_build_steps_requires_core_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "缺少必填字段：function"):
            build_steps({"repo": "./linux-7.0"}, Path("/tmp/axf-task"))

        with self.assertRaisesRegex(ValueError, "kRepo 产物或 AxF 后续流程"):
            build_steps({"repo": "./linux-7.0", "function": "can_send", "artifacts": []}, Path("/tmp/axf-task"))

    def test_default_config_targets_knowledge_base_outputs(self) -> None:
        defaults = default_config()

        self.assertEqual(defaults["function"], "can_send")
        self.assertIn(defaults["repo"], {"./linux-7.0", "../linux-7.0"})
        self.assertIn("report_json", defaults["artifacts"])
        self.assertIn("subsource", defaults["artifacts"])
        self.assertIn("harness_generation_agent", defaults["artifacts"])
        self.assertEqual(defaults["api_key_env"], "API_KEY")
        self.assertEqual(defaults["model_timeout"], 300)
        self.assertEqual(defaults["model_max_retries"], 2)

    def test_harness_step_adds_required_context_steps(self) -> None:
        steps = build_steps(
            {
                "repo": "./linux-7.0",
                "function": "can_send",
                "file": "net/can/af_can.c",
                "artifacts": ["harness_generation_agent"],
                "model": "glm-5.1",
                "chat_url": "https://example.invalid/v1/chat/completions",
                "api_key_env": "API_KEY",
                "model_max_retries": 2,
                "clang": "/opt/llvm/bin/clang",
                "max_repair_rounds": 5,
                "compile_timeout": 30,
            },
            Path("/tmp/axf-task"),
        )

        self.assertEqual(
            [step.name for step in steps],
            ["report_json", "subsource", "calls", "params", "harness_generation_agent"],
        )
        harness_command = steps[-1].command
        command_text = " ".join(harness_command)
        self.assertIn("-m agents.harness_generation.agent", command_text)
        self.assertIn("--model glm-5.1", command_text)
        self.assertIn("--api-key-env API_KEY", command_text)
        self.assertIn("--timeout 300", command_text)
        self.assertIn("--max-retries 2", command_text)
        self.assertIn("--clang /opt/llvm/bin/clang", command_text)
        self.assertIn("--max-repair-rounds 5", command_text)
        self.assertIn("--compile-timeout 30", command_text)
        self.assertEqual(steps[-1].artifact_path.name, "generated_harness.txt")

    def test_parse_model_json_accepts_fenced_json(self) -> None:
        payload = parse_model_json(
            """```json
{"classification":"byte_parser","files":[{"path":"harness.c","content":"int x;"}]}
```"""
        )

        self.assertEqual(payload["classification"], "byte_parser")
        self.assertEqual(payload["files"][0]["path"], "harness.c")

    def test_harness_agent_exposes_generated_files_as_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            step = PipelineStep(
                "harness_generation_agent",
                ["python", "-m", "agents.harness_generation.agent"],
                "harness_generation_agent",
                task_dir / "generated_harness.txt",
            )
            self.assertEqual(_extra_artifacts_for_step(step), [])

        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            harness_dir = task_dir / "harness"
            harness_dir.mkdir()
            (harness_dir / "harness.c").write_text("int x;\n", encoding="utf-8")
            (harness_dir / "harness_spec.json").write_text("{}\n", encoding="utf-8")
            (harness_dir / "compile.log").write_text("ok\n", encoding="utf-8")
            (harness_dir / "run.log").write_text("ok\n", encoding="utf-8")
            (harness_dir / "llm_transcript.md").write_text("# LLM\n", encoding="utf-8")
            step = PipelineStep(
                "harness_generation_agent",
                [],
                "harness_generation_agent",
                task_dir / "generated_harness.txt",
            )

            names = [name for name, _path in _extra_artifacts_for_step(step)]

        self.assertEqual(
            names,
            [
                "fuzz_harness",
                "harness_spec",
                "harness_compile_log",
                "harness_run_log",
                "harness_llm_transcript",
            ],
        )

    def test_harness_summary_exposes_compile_and_run_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            harness_dir = task_dir / "harness"
            harness_dir.mkdir()
            (harness_dir / "harness_spec.json").write_text(
                '{"status":"run_succeeded","classification":"byte_parser","compile":{"status":"success"},"run":{"status":"success","seconds":10}}\n',
                encoding="utf-8",
            )

            summary = _harness_summary(task_dir)

        self.assertEqual(summary["status"], "run_succeeded")
        self.assertEqual(summary["compile"]["status"], "success")
        self.assertEqual(summary["run"]["seconds"], 10)

    def test_harness_events_expose_compile_and_libfuzzer_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            harness_dir = task_dir / "harness"
            harness_dir.mkdir()
            (harness_dir / "harness_spec.json").write_text(
                (
                    '{"compile":{"status":"success","attempts":[{"attempt":1}]},'
                    '"run":{"status":"success","seconds":10,"returncode":0}}\n'
                ),
                encoding="utf-8",
            )
            step = PipelineStep(
                "harness_generation_agent",
                [],
                "harness_generation_agent",
                task_dir / "generated_harness.txt",
            )

            events = _harness_events_for_step(step)

        self.assertEqual([event["phase"] for event in events], ["harness_compile", "harness_run"])
        self.assertIn("编译通过", events[0]["message"])
        self.assertIn("libFuzzer 试跑通过", events[1]["message"])

    def test_compile_failed_harness_is_not_a_completed_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            harness_dir = task_dir / "harness"
            harness_dir.mkdir()
            (harness_dir / "harness_spec.json").write_text(
                '{"compile":{"status":"failed"},"status":"compile_failed"}\n',
                encoding="utf-8",
            )

            failure = _harness_failure_message(task_dir)
            message = _completion_message(task_dir)

        self.assertEqual(failure, "任务未完成：Harness 编译失败")
        self.assertEqual(message, "任务未完成：Harness 编译失败")

    def test_generated_harness_without_compile_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            harness_dir = task_dir / "harness"
            harness_dir.mkdir()
            (harness_dir / "harness_spec.json").write_text(
                '{"status":"generated"}\n',
                encoding="utf-8",
            )

            failure = _harness_failure_message(task_dir)

        self.assertEqual(failure, "任务未完成：Harness 仅生成，尚未编译验证")

    def test_harness_agent_without_spec_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            step = PipelineStep(
                "harness_generation_agent",
                [],
                "harness_generation_agent",
                task_dir / "generated_harness.txt",
            )

            failure = _harness_failure_message_for_step(step)

        self.assertEqual(failure, "任务未完成：Harness 生成 Agent 未写出 harness_spec.json")

    def test_run_succeeded_harness_has_no_failure_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            harness_dir = task_dir / "harness"
            harness_dir.mkdir()
            (harness_dir / "harness_spec.json").write_text(
                '{"status":"run_succeeded","compile":{"status":"success"},"run":{"status":"success"}}\n',
                encoding="utf-8",
            )

            failure = _harness_failure_message(task_dir)

        self.assertEqual(failure, "")


if __name__ == "__main__":
    unittest.main()
