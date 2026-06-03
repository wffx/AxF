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
    build_context,
    build_prompt,
    build_repair_prompt,
    compile_and_repair,
    compile_commands_for_mode,
    compile_harness,
    compile_skip_reason,
    html_response_error,
    merge_repair_payload,
    normalize_chat_url,
    parse_model_json,
    read_streaming_chat_response,
    request_harness_json,
    resolve_clang,
    run_harness,
    update_spec_compile,
    update_spec_run,
    wsl_shell_command,
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

    def test_parse_model_json_repairs_raw_newlines_inside_strings(self) -> None:
        content = '''```json
{
  "classification": "skb_handler",
  "files": [
    {"path": "dict.txt", "content": "\\\\x00
\\\\x01"}
  ]
}
```'''

        payload = parse_model_json(content)

        self.assertEqual(payload["classification"], "skb_handler")
        self.assertEqual(payload["files"][0]["content"], "\\x00\n\\x01")

    def test_parse_model_json_quotes_bare_object_keys(self) -> None:
        content = """
{
  classification: "skb_handler",
  files: [
    {path: "harness.c", content: "const char *s = \\"classification: keep\\";"}
  ],
  harness_spec: {status: "generated", function: {name: "can_send"}}
}
"""

        payload = parse_model_json(content)

        self.assertEqual(payload["classification"], "skb_handler")
        self.assertEqual(payload["files"][0]["path"], "harness.c")
        self.assertIn("classification: keep", payload["files"][0]["content"])
        self.assertEqual(payload["harness_spec"]["function"]["name"], "can_send")

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

    def test_request_harness_json_falls_back_when_stream_has_no_content(self) -> None:
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
            empty_stream = self.FakeResponse("", [b"event: ping\n\n", b"data: [DONE]\n\n"])
            plain_response = self.FakeResponse(
                '{"choices":[{"message":{"content":"{\\"classification\\":\\"byte_parser\\",\\"files\\":[]}"}}]}'
            )

            with mock.patch("agents.harness_generation.agent.urllib.request.urlopen", side_effect=[empty_stream, plain_response]) as urlopen:
                payload = request_harness_json(
                    prompt="生成 harness",
                    args=args,
                    transcript_path=transcript,
                    interaction="initial generation",
                )

            first_body = json.loads(urlopen.call_args_list[0].args[0].data.decode("utf-8"))
            second_body = json.loads(urlopen.call_args_list[1].args[0].data.decode("utf-8"))

        self.assertEqual(payload["classification"], "byte_parser")
        self.assertTrue(first_body["stream"])
        self.assertFalse(second_body["stream"])

    def test_request_harness_json_retries_invalid_json_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"API_KEY": "secret"}):
            transcript = Path(tmp) / "llm_transcript.md"
            args = argparse.Namespace(
                model="glm-5.1",
                chat_url="https://example.invalid/v1",
                api_key_env="API_KEY",
                timeout=300,
                max_retries=0,
                no_stream=True,
            )
            malformed = self.FakeResponse(
                '{"choices":[{"message":{"content":"{\\"classification\\":\\"skb_handler\\",\\"files\\":[{\\"path\\":\\"dict.txt\\",\\"content\\":\\"bad"}}]}'
            )
            repaired = self.FakeResponse(
                '{"choices":[{"message":{"content":"{\\"classification\\":\\"skb_handler\\",\\"files\\":[]}"}}]}'
            )

            with mock.patch("agents.harness_generation.agent.urllib.request.urlopen", side_effect=[malformed, repaired]) as urlopen:
                payload = request_harness_json(
                    prompt="生成 harness",
                    args=args,
                    transcript_path=transcript,
                    interaction="initial generation",
                )

            second_body = json.loads(urlopen.call_args_list[1].args[0].data.decode("utf-8"))
            text = transcript.read_text(encoding="utf-8")

        self.assertEqual(payload["classification"], "skb_handler")
        self.assertFalse(second_body["stream"])
        self.assertIn("JSON retry", text)

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

    def test_read_streaming_chat_response_accepts_plain_chat_json(self) -> None:
        response = self.FakeResponse(
            "",
            [
                b'{"choices":[{"message":{"content":"{\\"ok\\":true}"}}]}',
            ],
        )

        self.assertEqual(read_streaming_chat_response(response), '{"ok":true}')

    def test_normalize_chat_url_accepts_base_or_full_endpoint(self) -> None:
        self.assertEqual(
            normalize_chat_url("https://example.invalid"),
            "https://example.invalid/v1/chat/completions",
        )
        self.assertEqual(
            normalize_chat_url("https://example.invalid/api"),
            "https://example.invalid/api/v1/chat/completions",
        )
        self.assertEqual(
            normalize_chat_url("https://example.invalid/api/v1"),
            "https://example.invalid/api/v1/chat/completions",
        )
        self.assertEqual(
            normalize_chat_url("https://example.invalid/api/v1/chat/completions"),
            "https://example.invalid/api/v1/chat/completions",
        )

    def test_html_response_error_points_to_bad_endpoint(self) -> None:
        message = html_response_error(
            '<!doctype html><html lang="zh"><head><title>New API</title></head></html>',
            "https://example.invalid",
        )

        self.assertIn("HTML 网页", message)
        self.assertIn("不是 Chat Completions JSON", message)
        self.assertIn("https://example.invalid", message)

    def test_build_context_accepts_no_krepo_prompt_context(self) -> None:
        args = argparse.Namespace(
            function="can_send",
            file="net/can/af_can.c",
            repo="./linux-7.0",
            task_dir="/tmp/axf-task",
            report_json="",
            subsource="",
            calls="",
            params="",
        )

        context = build_context(args)
        prompt = build_prompt(context)

        self.assertEqual(context["report_json"], "")
        self.assertNotIn("--- report.json ---", prompt)
        self.assertNotIn("--- subsource bundle ---", prompt)
        self.assertIn("未选择额外 kRepo 知识产物", prompt)
        self.assertIn("不要仅因缺少上下文而标记 unsupported", prompt)
        self.assertNotIn("信息不足时标记 unsupported", prompt)

    def test_build_context_uses_only_explicit_krepo_prompt_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.json"
            report.write_text('{"name":"can_send"}\n', encoding="utf-8")
            args = argparse.Namespace(
                function="can_send",
                file="net/can/af_can.c",
                repo="./linux-7.0",
                task_dir="/tmp/axf-task",
                report_json=str(report),
                subsource="",
                calls="",
                params="",
            )

            prompt = build_prompt(build_context(args))

        self.assertIn("--- report.json ---", prompt)
        self.assertIn('{"name":"can_send"}', prompt)
        self.assertNotIn("--- upstream calls ---", prompt)
        self.assertNotIn("--- parameter constraints ---", prompt)

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

    def test_compile_harness_falls_back_to_lighter_sanitizers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_clang = root / "fake-clang"
            fake_clang.write_text(
                "#!/bin/sh\n"
                "echo compiling \"$@\"\n"
                "case \"$*\" in\n"
                "  *fuzzer,address,undefined*) echo missing runtime; exit 1 ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_clang.chmod(0o755)
            (root / "harness.c").write_text(
                "int LLVMFuzzerTestOneInput(const unsigned char *d, unsigned long s){return 0;}\n",
                encoding="utf-8",
            )
            (root / "mocks.c").write_text("", encoding="utf-8")
            args = argparse.Namespace(clang=str(fake_clang), compile_timeout=5)

            result = compile_harness(root, args, 1)

        self.assertTrue(result.ok)
        self.assertIn("-fsanitize=fuzzer,address", result.command)
        self.assertIn("variant 1/3", result.output)
        self.assertIn("variant 2/3", result.output)
        self.assertIn("missing runtime", result.output)

    def test_wsl_compile_mode_uses_linux_binary_and_wsl_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "compile.log"
            args = argparse.Namespace(clang="/usr/bin/clang", clang_mode="wsl", compile_timeout=5)

            with mock.patch("agents.harness_generation.agent.shutil.which", return_value="/Windows/System32/wsl.exe"):
                with mock.patch("agents.harness_generation.agent.resolve_wsl_path", return_value=("/mnt/c/AxF/task", "")):
                    commands = compile_commands_for_mode(root, "/usr/bin/clang", "fuzzer", "wsl", log_path, 1, args)

        self.assertIsInstance(commands, list)
        command = commands[0]
        command_text = " ".join(command)
        self.assertEqual(command[:3], ["wsl", "sh", "-lc"])
        self.assertIn("cd /mnt/c/AxF/task", command_text)
        self.assertIn("/usr/bin/clang", command_text)
        self.assertIn("-o fuzzer", command_text)
        self.assertNotIn("fuzzer.exe", command_text)

    def test_wsl_mode_defaults_to_usr_bin_clang(self) -> None:
        args = argparse.Namespace(clang="", clang_mode="wsl")

        self.assertEqual(resolve_clang(args), "/usr/bin/clang")

    def test_wsl_shell_command_quotes_windows_task_paths(self) -> None:
        command = wsl_shell_command("/mnt/c/Users/yufei/Documents/llm fuzzing/AxF/task", ["/usr/bin/clang", "harness.c"])

        self.assertEqual(command[:3], ["wsl", "sh", "-lc"])
        self.assertIn("'/mnt/c/Users/yufei/Documents/llm fuzzing/AxF/task'", command[3])

    def test_compile_skip_reason_for_missing_harness_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIn("harness.c", compile_skip_reason(root, {"harness_spec": {"status": "generated"}}))
            reason = compile_skip_reason(root, {"harness_spec": {"status": "unsupported"}})
            self.assertIn("unsupported", reason)

            (root / "harness.c").write_text("int x;\n", encoding="utf-8")
            reason_with_harness = compile_skip_reason(root, {"harness_spec": {"status": "unsupported"}})

        self.assertEqual(reason_with_harness, "")

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
