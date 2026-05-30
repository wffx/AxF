from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents.harness_generation.agent import (
    CompileResult,
    HarnessGenerationError,
    RunResult,
    append_llm_transcript,
    build_repair_prompt,
    compile_and_repair,
    compile_harness,
    compile_skip_reason,
    merge_repair_payload,
    normalize_chat_url,
    read_streaming_chat_response,
    request_harness_json,
    run_harness,
    update_spec_compile,
    update_spec_run,
)


class HarnessGenerationAgentTest(unittest.TestCase):
    class FakeResponse:
        def __init__(self, text: str, lines: list[bytes] | None = None):
            self.text = text
            self.lines = lines or []

        def __enter__(self) -> "HarnessGenerationAgentTest.FakeResponse":
            return self

        def __exit__(self, _exc_type, _exc, _traceback) -> None:
            return None

        def read(self) -> bytes:
            return self.text.encode("utf-8")

        def __iter__(self):
            return iter(self.lines)

    def test_append_llm_transcript_records_messages_without_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "llm_transcript.md"

            append_llm_transcript(
                path,
                interaction="initial generation",
                model="glm-5.1",
                messages=[
                    {"role": "system", "content": "system says"},
                    {"role": "user", "content": "user says"},
                ],
                assistant='{"files":[]}',
            )

            text = path.read_text(encoding="utf-8")

        self.assertIn("initial generation", text)
        self.assertIn("system says", text)
        self.assertIn("user says", text)
        self.assertIn('{"files":[]}', text)
        self.assertNotIn("API_KEY", text)

    def test_request_harness_json_retries_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"API_KEY": "secret"}):
            transcript = Path(tmp) / "llm_transcript.md"
            args = argparse.Namespace(
                model="glm-5.1",
                chat_url="https://example.invalid/v1/chat/completions",
                api_key_env="API_KEY",
                timeout=300,
                max_retries=1,
                no_stream=True,
            )
            response = self.FakeResponse(
                '{"choices":[{"message":{"content":"{\\"classification\\":\\"byte_parser\\",\\"files\\":[]}"}}]}'
            )

            with mock.patch("agents.harness_generation.agent.urllib.request.urlopen", side_effect=[TimeoutError("slow"), response]) as urlopen:
                payload = request_harness_json(
                    prompt="生成 harness",
                    args=args,
                    transcript_path=transcript,
                    interaction="initial generation",
                )

            text = transcript.read_text(encoding="utf-8")

        self.assertEqual(payload["classification"], "byte_parser")
        self.assertEqual(urlopen.call_count, 2)
        self.assertIn("第 1/2 次", text)
        self.assertIn("assistant", text)

    def test_request_harness_json_accepts_streaming_chat_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"API_KEY": "secret"}):
            transcript = Path(tmp) / "llm_transcript.md"
            args = argparse.Namespace(
                model="glm-5.1",
                chat_url="https://example.invalid/v1",
                api_key_env="API_KEY",
                timeout=300,
                max_retries=0,
                no_stream=False,
            )
            response = self.FakeResponse(
                "",
                [
                    b'data: {"choices":[{"delta":{"content":"{\\"classification\\": "}}]}\n\n',
                    b'data: {"choices":[{"delta":{"content":"\\"byte_parser\\", \\"files\\":[]}"}}]}\n\n',
                    b"data: [DONE]\n\n",
                ],
            )

            with mock.patch("agents.harness_generation.agent.urllib.request.urlopen", return_value=response) as urlopen:
                payload = request_harness_json(
                    prompt="生成 harness",
                    args=args,
                    transcript_path=transcript,
                    interaction="initial generation",
                )

            request = urlopen.call_args.args[0]
            body = json.loads(request.data.decode("utf-8"))

        self.assertEqual(payload["classification"], "byte_parser")
        self.assertEqual(request.full_url, "https://example.invalid/v1/chat/completions")
        self.assertTrue(body["stream"])

    def test_read_streaming_chat_response_joins_sse_chunks(self) -> None:
        response = self.FakeResponse(
            "",
            [
                b"event: ignored\n\n",
                b'data: {"choices":[{"delta":{"content":"{\\"ok\\":"}}]}\n\n',
                b'data: {"choices":[{"delta":{"content":"true}"}}]}\n\n',
                b"data: [DONE]\n\n",
            ],
        )

        self.assertEqual(read_streaming_chat_response(response), '{"ok":true}')

    def test_normalize_chat_url_accepts_base_or_full_endpoint(self) -> None:
        self.assertEqual(
            normalize_chat_url("https://example.invalid/api/v1"),
            "https://example.invalid/api/v1/chat/completions",
        )
        self.assertEqual(
            normalize_chat_url("https://example.invalid/api/v1/chat/completions"),
            "https://example.invalid/api/v1/chat/completions",
        )

    def test_compile_harness_records_success_with_configured_clang(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_clang = root / "fake-clang"
            fake_clang.write_text("#!/bin/sh\necho compiling \"$@\"\nexit 0\n", encoding="utf-8")
            fake_clang.chmod(0o755)
            (root / "harness.c").write_text("int LLVMFuzzerTestOneInput(const unsigned char *d, unsigned long s){return 0;}\n", encoding="utf-8")
            (root / "mocks.c").write_text("", encoding="utf-8")
            args = argparse.Namespace(clang=str(fake_clang), compile_timeout=5)

            result = compile_harness(root, args, 1)

        self.assertTrue(result.ok)
        self.assertEqual(result.returncode, 0)
        self.assertIn("compiling", result.output)

    def test_compile_skip_reason_for_unsupported_or_missing_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIn("harness.c", compile_skip_reason(root, {"harness_spec": {"status": "generated"}}))
            (root / "harness.c").write_text("int x;\n", encoding="utf-8")

            reason = compile_skip_reason(root, {"harness_spec": {"status": "unsupported"}})

        self.assertIn("unsupported", reason)

    def test_merge_repair_payload_keeps_unchanged_files(self) -> None:
        base = {
            "classification": "skb_handler",
            "files": [
                {"path": "harness.c", "content": "old harness"},
                {"path": "mocks.h", "content": "old header"},
            ],
            "harness_spec": {"status": "generated", "diagnostics": ["initial"]},
        }
        repair = {
            "mock_rationale": "add missing bool",
            "files": [{"path": "mocks.h", "content": "#include <stdbool.h>\n"}],
            "harness_spec": {"diagnostics": ["fixed missing bool"]},
        }

        merged = merge_repair_payload(base, repair)
        files = {item["path"]: item["content"] for item in merged["files"]}

        self.assertEqual(merged["classification"], "skb_handler")
        self.assertEqual(files["harness.c"], "old harness")
        self.assertEqual(files["mocks.h"], "#include <stdbool.h>\n")
        self.assertIn("initial", merged["harness_spec"]["diagnostics"])
        self.assertIn("fixed missing bool", merged["harness_spec"]["diagnostics"])

    def test_repair_prompt_is_lightweight_and_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "harness.c").write_text("int harness;\n", encoding="utf-8")
            (root / "mocks.h").write_text("int header;\n", encoding="utf-8")
            (root / "mocks.c").write_text("int mock;\n", encoding="utf-8")
            (root / "build.sh").write_text("should not be included\n", encoding="utf-8")
            (root / "harness_spec.json").write_text('{"status":"generated"}\n', encoding="utf-8")
            result = CompileResult(
                attempt=1,
                command=["clang", "harness.c", "mocks.c"],
                returncode=1,
                output="error: use of undeclared identifier false",
                log_path=root / "compile_attempt_1.log",
            )

            prompt = build_repair_prompt(
                {"file": "net/can/af_can.c", "function": "can_send", "params": "param info", "calls": "call info"},
                root,
                result,
                1,
                3,
            )

        self.assertIn("只返回需要修改的文件", prompt)
        self.assertIn("mocks.h", prompt)
        self.assertIn("use of undeclared identifier false", prompt)
        self.assertNotIn("should not be included", prompt)

    def test_update_spec_compile_writes_status_and_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "compile_attempt_1.log"
            log_path.write_text("error\n", encoding="utf-8")
            payload = {"harness_spec": {"status": "generated", "diagnostics": []}}
            result = CompileResult(
                attempt=1,
                command=["clang", "harness.c"],
                returncode=1,
                output="error",
                log_path=log_path,
            )

            update_spec_compile(root, payload, "failed", [result], "compile failed")

            spec = payload["harness_spec"]
            self.assertEqual(spec["status"], "compile_failed")
            self.assertEqual(spec["compile"]["status"], "failed")
            self.assertEqual(spec["compile"]["attempts"][0]["returncode"], 1)

    def test_run_harness_records_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fuzzer = root / "fuzzer"
            fuzzer.write_text("#!/bin/sh\necho running \"$@\"\nexit 0\n", encoding="utf-8")
            fuzzer.chmod(0o755)
            args = argparse.Namespace(run_seconds=10)

            result = run_harness(root, args)

        self.assertTrue(result.ok)
        self.assertEqual(result.returncode, 0)
        self.assertIn("-max_total_time=10", result.command)
        self.assertIn("running", result.output)

    def test_update_spec_run_writes_runtime_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "run.log"
            log_path.write_text("ok\n", encoding="utf-8")
            payload = {"harness_spec": {"status": "compiled", "diagnostics": []}}
            result = RunResult(
                command=["./fuzzer", "-max_total_time=10"],
                returncode=0,
                output="ok",
                log_path=log_path,
                seconds=10,
            )

            update_spec_run(root, payload, result, "success", "10 second run succeeded")

            spec = payload["harness_spec"]
            self.assertEqual(spec["status"], "run_succeeded")
            self.assertEqual(spec["run"]["status"], "success")
            self.assertEqual(spec["run"]["seconds"], 10)

    def test_repair_timeout_becomes_compile_failed_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_clang = root / "fake-clang"
            fake_clang.write_text("#!/bin/sh\necho broken\nexit 1\n", encoding="utf-8")
            fake_clang.chmod(0o755)
            (root / "harness.c").write_text("int broken;\n", encoding="utf-8")
            (root / "mocks.c").write_text("", encoding="utf-8")
            payload = {"harness_spec": {"status": "generated", "diagnostics": []}}
            args = argparse.Namespace(
                skip_compile=False,
                max_repair_rounds=1,
                clang=str(fake_clang),
                compile_timeout=5,
            )

            with mock.patch(
                "agents.harness_generation.agent.request_harness_json",
                side_effect=HarnessGenerationError("模型请求超时（300 秒）"),
            ):
                payload, written = compile_and_repair(
                    args,
                    root,
                    payload,
                    {"file": "net/can/af_can.c", "function": "can_send", "params": "", "calls": ""},
                    [root / "harness.c", root / "mocks.c"],
                )

            spec = payload["harness_spec"]
            self.assertEqual(spec["status"], "compile_failed")
            self.assertEqual(spec["compile"]["status"], "failed")
            self.assertIn("LLM repair request failed", spec["compile"]["message"])
            self.assertIn(root / "compile.log", written)


if __name__ == "__main__":
    unittest.main()
