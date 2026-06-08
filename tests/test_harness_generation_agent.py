from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import tempfile
import urllib.error
import unittest
from pathlib import Path
from unittest import mock

from agents.harness_generation.agent import (
    CompileResult,
    CoverageResult,
    HarnessGenerationError,
    PROJECT_ROOT,
    RunResult,
    append_llm_transcript,
    build_opencode_command,
    build_context,
    build_prompt,
    build_repair_prompt,
    compile_and_repair,
    compile_commands_for_mode,
    compile_harness,
    compile_skip_reason,
    coverage_compact_summary,
    ensure_required_harness_payload,
    html_response_error,
    merge_repair_payload,
    normalize_chat_url,
    parse_model_json,
    read_streaming_chat_response,
    record_llm_transcript,
    request_harness_json,
    resolve_clang,
    resolve_cli_executable,
    run_harness,
    is_retryable_url_error,
    update_spec_coverage,
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

    def test_record_llm_transcript_skips_non_harness_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "llm_transcript.md"

            record_llm_transcript(
                path,
                record_only_harness=True,
                interaction="initial generation",
                model="nga-default",
                messages=[{"role": "user", "content": "生成 harness"}],
                assistant='{"classification":"needs_manual_fixture","files":[]}',
            )

            exists = path.exists()

        self.assertFalse(exists)

    def test_record_llm_transcript_records_harness_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "llm_transcript.md"

            record_llm_transcript(
                path,
                record_only_harness=True,
                interaction="initial generation",
                model="nga-default",
                messages=[{"role": "user", "content": "生成 harness"}],
                assistant='{"classification":"byte_parser","files":[{"path":"harness.c","content":"int x;"}]}',
            )

            text = path.read_text(encoding="utf-8")

        self.assertIn("initial generation", text)
        self.assertIn("harness.c", text)

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

    def test_ssl_eof_url_error_is_retryable(self) -> None:
        error = urllib.error.URLError(
            ssl.SSLEOFError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
        )

        self.assertTrue(is_retryable_url_error(error))

    def test_build_opencode_command_uses_nga_run_dir_and_model(self) -> None:
        with mock.patch("agents.harness_generation.agent.shutil.which", return_value="nga"):
            command = build_opencode_command(
                tool="nga",
                executable="nga",
                repo=".",
                prompt="生成 harness",
                model="anthropic/claude-sonnet-4",
            )

        self.assertEqual(command[:4], ["nga", "run", "--dir", str(PROJECT_ROOT)])
        self.assertEqual(command[4:6], ["--model", "anthropic/claude-sonnet-4"])
        self.assertEqual(command[-1], "生成 harness")

    def test_resolve_cli_executable_requires_windows_path_or_path_entry(self) -> None:
        with (
            mock.patch("agents.harness_generation.agent.sys.platform", "win32"),
            mock.patch("agents.harness_generation.agent.shutil.which", return_value=None),
        ):
            with self.assertRaisesRegex(FileNotFoundError, "C:/tools/nga.cmd"):
                resolve_cli_executable("nga", "nga")

    def test_build_opencode_command_supports_claude_cli_shape(self) -> None:
        with mock.patch("agents.harness_generation.agent.shutil.which", return_value="claude"):
            command = build_opencode_command(
                tool="claude",
                executable="claude",
                repo=".",
                prompt="生成 harness",
                model="sonnet",
            )

        self.assertEqual(command, ["claude", "-p", "--model", "sonnet", "生成 harness"])

    def test_request_harness_json_invokes_opencode_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "llm_transcript.md"
            args = argparse.Namespace(
                repo=".",
                llm_mode="opencode",
                opencode_tool="nga",
                opencode_executable="nga",
                opencode_model="anthropic/claude-sonnet-4",
                model="",
                timeout=300,
                max_retries=0,
            )
            completed = subprocess.CompletedProcess(
                ["opencode"],
                0,
                stdout='{"classification":"byte_parser","files":[]}',
                stderr="",
            )

            with (
                mock.patch("agents.harness_generation.agent.shutil.which", return_value="nga"),
                mock.patch("agents.harness_generation.agent.subprocess.run", return_value=completed) as run,
            ):
                payload = request_harness_json(
                    prompt="生成 harness",
                    args=args,
                    transcript_path=transcript,
                    interaction="initial generation",
                )

            command = run.call_args.args[0]
            text = transcript.read_text(encoding="utf-8")
            workspace_prompt_text = (Path(command[3]) / "context" / "prompt.md").read_text(encoding="utf-8")

        self.assertEqual(payload["classification"], "byte_parser")
        self.assertEqual(command[:3], ["nga", "run", "--dir"])
        self.assertTrue(command[3].endswith("opencode_workspace"))
        self.assertEqual(command[4:6], ["--model", "anthropic/claude-sonnet-4"])
        self.assertIn("context/prompt.md", command[-1])
        self.assertEqual(workspace_prompt_text, "生成 harness")
        self.assertIn("anthropic/claude-sonnet-4", text)
        self.assertIn('"classification":"byte_parser"', text)

    def test_request_harness_json_record_only_harness_skips_non_harness_opencode_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "llm_transcript.md"
            args = argparse.Namespace(
                repo=".",
                llm_mode="opencode",
                opencode_tool="opencode",
                opencode_executable="opencode",
                opencode_model="",
                model="",
                timeout=300,
                max_retries=0,
            )
            completed = subprocess.CompletedProcess(
                ["opencode"],
                0,
                stdout='{"classification":"needs_manual_fixture","files":[]}',
                stderr="",
            )

            with (
                mock.patch("agents.harness_generation.agent.shutil.which", return_value="opencode"),
                mock.patch("agents.harness_generation.agent.subprocess.run", return_value=completed),
            ):
                payload = request_harness_json(
                    prompt="生成 harness",
                    args=args,
                    transcript_path=transcript,
                    interaction="initial generation",
                    record_only_harness=True,
                )

            exists = transcript.exists()

        self.assertEqual(payload["classification"], "needs_manual_fixture")
        self.assertFalse(exists)

    def test_request_harness_json_record_only_harness_records_all_nga_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "llm_transcript.md"
            args = argparse.Namespace(
                repo=".",
                llm_mode="opencode",
                opencode_tool="nga",
                opencode_executable="nga",
                opencode_model="",
                model="",
                timeout=300,
                max_retries=0,
            )
            completed = subprocess.CompletedProcess(
                ["nga"],
                0,
                stdout='{"classification":"needs_manual_fixture","files":[]}',
                stderr="nga diagnostic line",
            )

            with (
                mock.patch("agents.harness_generation.agent.shutil.which", return_value="nga"),
                mock.patch("agents.harness_generation.agent.subprocess.run", return_value=completed),
            ):
                payload = request_harness_json(
                    prompt="生成 harness",
                    args=args,
                    transcript_path=transcript,
                    interaction="initial generation",
                    record_only_harness=True,
                )

            text = transcript.read_text(encoding="utf-8")
            interaction_dir = Path(tmp) / "nga_interactions" / "01_initial_generation"
            stdout_text = (interaction_dir / "stdout.rawoutput").read_text(encoding="utf-8")
            stderr_text = (interaction_dir / "stderr.rawoutput").read_text(encoding="utf-8")
            combined_text = (interaction_dir / "combined.rawoutput").read_text(encoding="utf-8")
            prompt_text = (interaction_dir / "prompt.md").read_text(encoding="utf-8")
            metadata = json.loads((interaction_dir / "metadata.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["classification"], "needs_manual_fixture")
        self.assertIn("initial generation", text)
        self.assertIn("needs_manual_fixture", text)
        self.assertIn("nga diagnostic line", text)
        self.assertIn("needs_manual_fixture", stdout_text)
        self.assertEqual(stderr_text, "nga diagnostic line")
        self.assertIn("nga diagnostic line", combined_text)
        self.assertEqual(prompt_text, "生成 harness")
        self.assertEqual(metadata["returncode"], 0)
        self.assertEqual(metadata["command"][:3], ["nga", "run", "--dir"])

    def test_request_harness_json_record_only_harness_records_nga_cli_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "llm_transcript.md"
            args = argparse.Namespace(
                repo=".",
                llm_mode="opencode",
                opencode_tool="nga",
                opencode_executable="nga",
                opencode_model="",
                model="",
                timeout=300,
                max_retries=0,
            )
            completed = subprocess.CompletedProcess(
                ["nga"],
                1,
                stdout="partial stdout",
                stderr="permission requested",
            )

            with (
                mock.patch("agents.harness_generation.agent.shutil.which", return_value="nga"),
                mock.patch("agents.harness_generation.agent.subprocess.run", return_value=completed),
            ):
                with self.assertRaisesRegex(HarnessGenerationError, "nga CLI 退出码"):
                    request_harness_json(
                        prompt="生成 harness",
                        args=args,
                        transcript_path=transcript,
                        interaction="initial generation",
                        record_only_harness=True,
                    )

            text = transcript.read_text(encoding="utf-8")
            interaction_dir = Path(tmp) / "nga_interactions" / "01_initial_generation"
            stdout_text = (interaction_dir / "stdout.rawoutput").read_text(encoding="utf-8")
            stderr_text = (interaction_dir / "stderr.rawoutput").read_text(encoding="utf-8")
            combined_text = (interaction_dir / "combined.rawoutput").read_text(encoding="utf-8")
            metadata = json.loads((interaction_dir / "metadata.json").read_text(encoding="utf-8"))

        self.assertIn("partial stdout", text)
        self.assertIn("permission requested", text)
        self.assertIn("nga CLI 退出码：1", text)
        self.assertEqual(stdout_text, "partial stdout")
        self.assertEqual(stderr_text, "permission requested")
        self.assertIn("partial stdout", combined_text)
        self.assertIn("permission requested", combined_text)
        self.assertEqual(metadata["returncode"], 1)
        self.assertEqual(metadata["error"], "nga CLI 退出码：1")

    def test_request_harness_json_uses_isolated_workspace_for_opencode_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "llm_transcript.md"
            args = argparse.Namespace(
                repo=".",
                llm_mode="opencode",
                opencode_tool="nga",
                opencode_executable="nga",
                opencode_model="",
                model="",
                timeout=300,
                max_retries=0,
            )
            completed = subprocess.CompletedProcess(
                ["opencode"],
                0,
                stdout='{"classification":"byte_parser","files":[]}',
                stderr="",
            )
            prompt = "生成 harness\n" + ("x" * 9000)

            with (
                mock.patch("agents.harness_generation.agent.shutil.which", return_value="nga"),
                mock.patch("agents.harness_generation.agent.subprocess.run", return_value=completed) as run,
            ):
                payload = request_harness_json(
                    prompt=prompt,
                    args=args,
                    transcript_path=transcript,
                    interaction="initial generation",
                )

            command = run.call_args.args[0]
            workspace_dir = Path(command[3])
            workspace_exists = workspace_dir.is_dir()
            prompt_file_text = (workspace_dir / "context" / "prompt.md").read_text(encoding="utf-8")
            request_file_text = (workspace_dir / "context" / "prompt_request_1.md").read_text(encoding="utf-8")

        self.assertEqual(payload["classification"], "byte_parser")
        self.assertNotIn("--file", command)
        self.assertNotIn(prompt, command)
        self.assertTrue(workspace_exists)
        self.assertEqual(workspace_dir.name, "opencode_workspace")
        self.assertIn("context/prompt.md", command[-1])
        self.assertEqual(prompt_file_text, prompt)
        self.assertEqual(request_file_text, prompt)

    def test_request_harness_json_uses_strict_cli_json_retry_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "llm_transcript.md"
            args = argparse.Namespace(
                repo=".",
                llm_mode="opencode",
                opencode_tool="nga",
                opencode_executable="nga",
                opencode_model="",
                model="",
                timeout=300,
                max_retries=1,
            )
            malformed = subprocess.CompletedProcess(
                ["nga"],
                0,
                stdout="model: minimax\n```json\nnot-json\n```\nbuild info",
                stderr="",
            )
            repaired = subprocess.CompletedProcess(
                ["nga"],
                0,
                stdout='{"classification":"byte_parser","files":[]}',
                stderr="",
            )

            with (
                mock.patch("agents.harness_generation.agent.shutil.which", return_value="nga"),
                mock.patch("agents.harness_generation.agent.subprocess.run", side_effect=[malformed, repaired]),
            ):
                payload = request_harness_json(
                    prompt="生成 harness",
                    args=args,
                    transcript_path=transcript,
                    interaction="initial generation",
                )

            retry_prompt = (Path(tmp) / "opencode_workspace" / "context" / "prompt_request_2.md").read_text(encoding="utf-8")

        self.assertEqual(payload["classification"], "byte_parser")
        self.assertIn("上一轮 nga 输出不是合法 JSON，解析错误：", retry_prompt)
        self.assertIn("请重新输出一个且仅一个完整 JSON 对象", retry_prompt)
        self.assertIn("必须以 { 开头并以 } 结尾", retry_prompt)
        self.assertIn("可被 Python json.loads 直接解析", retry_prompt)
        self.assertIn("不要输出 Markdown、代码块、注释、解释、日志或任何 JSON 之外的字符", retry_prompt)

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

    def test_parse_model_json_accepts_json_like_single_quotes_and_trailing_commas(self) -> None:
        content = """
{
  'classification': 'skb_handler',
  files: [
    {'path': 'harness.c', 'content': 'int LLVMFuzzerTestOneInput(const unsigned char *d, unsigned long s){return 0;}'},
  ],
  harness_spec: {'status': 'generated',},
}
"""

        payload = parse_model_json(content)

        self.assertEqual(payload["classification"], "skb_handler")
        self.assertEqual(payload["files"][0]["path"], "harness.c")
        self.assertEqual(payload["harness_spec"]["status"], "generated")

    def test_parse_model_json_ignores_text_after_first_complete_object(self) -> None:
        content = (
            '{"classification":"needs_manual_fixture","files":[]}'
            "\nnga 输出不是合法 JSON：Expecting value: line 1 column 1 (char 0)"
        )

        payload = parse_model_json(content)

        self.assertEqual(payload["classification"], "needs_manual_fixture")
        self.assertEqual(payload["files"], [])

    def test_parse_model_json_extracts_first_object_from_markdown_log(self) -> None:
        content = """
### assistant

```text
{"classification":"byte_parser","files":[{"path":"harness.c","content":"int x;"}]}
```

retry log follows
"""

        payload = parse_model_json(content)

        self.assertEqual(payload["classification"], "byte_parser")
        self.assertEqual(payload["files"][0]["path"], "harness.c")

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

    def test_request_harness_json_uses_retry_budget_for_invalid_json_repairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"API_KEY": "secret"}):
            transcript = Path(tmp) / "llm_transcript.md"
            args = argparse.Namespace(
                model="glm-5.1",
                chat_url="https://example.invalid/v1",
                api_key_env="API_KEY",
                timeout=300,
                max_retries=2,
                no_stream=True,
            )
            malformed = self.FakeResponse(
                '{"choices":[{"message":{"content":"{\\"classification\\":\\"skb_handler\\",\\"files\\":[{\\"path\\":\\"dict.txt\\",\\"content\\":\\"bad"}}]}'
            )
            malformed_repair = self.FakeResponse(
                '{"choices":[{"message":{"content":"{\\"classification\\":\\"skb_handler\\",\\"files\\":[{\\"path\\":\\"dict.txt\\",\\"content\\":\\"still-bad"}}]}'
            )
            repaired = self.FakeResponse(
                '{"choices":[{"message":{"content":"{\\"classification\\":\\"skb_handler\\",\\"files\\":[]}"}}]}'
            )

            with mock.patch("agents.harness_generation.agent.urllib.request.urlopen", side_effect=[malformed, malformed_repair, repaired]) as urlopen:
                payload = request_harness_json(
                    prompt="生成 harness",
                    args=args,
                    transcript_path=transcript,
                    interaction="initial generation",
                )

            text = transcript.read_text(encoding="utf-8")

        self.assertEqual(payload["classification"], "skb_handler")
        self.assertEqual(urlopen.call_count, 3)
        self.assertIn("initial generation JSON retry 1/2", text)
        self.assertIn("initial generation JSON retry 2/2", text)

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
        self.assertIn("不是错误", prompt)
        self.assertIn("不是必须存在的外部知识库路径", prompt)
        self.assertIn("不要回答", prompt)
        self.assertIn("目标函数信息已经在上方给出", prompt)
        self.assertIn("需要以下信息：目标函数信息", prompt)
        self.assertIn("不要仅因缺少上下文而标记 unsupported", prompt)
        self.assertNotIn("信息不足时标记 unsupported", prompt)

    def test_missing_harness_payload_retries_context_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "llm_transcript.md"
            args = argparse.Namespace()
            first = {
                "classification": "unsupported",
                "unsupported_reason": "没有找到 kRepo/AxF 知识产物和 harness 生成规则知识库路径",
                "files": [],
            }
            second = {
                "classification": "byte_parser",
                "files": [{"path": "harness.c", "content": "int LLVMFuzzerTestOneInput(const unsigned char *d, unsigned long s){return 0;}"}],
            }

            with mock.patch("agents.harness_generation.agent.request_harness_json", return_value=second) as request:
                payload = ensure_required_harness_payload(
                    payload=first,
                    prompt="生成 harness",
                    args=args,
                    transcript_path=transcript,
                )

            retry_prompt = request.call_args.kwargs["prompt"]

        self.assertEqual(payload, second)
        self.assertIn("缺少 kRepo/AxF 知识产物", retry_prompt)
        self.assertIn("必须包含 harness.c", retry_prompt)

    def test_missing_harness_payload_retries_target_info_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "llm_transcript.md"
            first = {
                "classification": "needs_manual_fixture",
                "unsupported_reason": "需要以下信息：目标函数信息、AxF/kRepo知识产物",
                "files": [],
            }
            second = {
                "classification": "byte_parser",
                "files": [{"path": "harness.c", "content": "int LLVMFuzzerTestOneInput(const unsigned char *d, unsigned long s){return 0;}"}],
            }

            with mock.patch("agents.harness_generation.agent.request_harness_json", return_value=second) as request:
                payload = ensure_required_harness_payload(
                    payload=first,
                    prompt="生成 harness",
                    args=argparse.Namespace(),
                    transcript_path=transcript,
                )

            retry_prompt = request.call_args.kwargs["prompt"]

        self.assertEqual(payload, second)
        self.assertIn("缺少目标函数信息", retry_prompt)
        self.assertIn("目标函数信息已经给出", retry_prompt)

    def test_missing_harness_payload_allows_real_unsupported_reason(self) -> None:
        payload = {
            "classification": "unsupported",
            "unsupported_reason": "requires real hardware DMA engine and unmockable interrupt timing",
            "files": [],
        }

        result = ensure_required_harness_payload(
            payload=payload,
            prompt="生成 harness",
            args=argparse.Namespace(),
            transcript_path=Path("/tmp/llm_transcript.md"),
        )

        self.assertIs(result, payload)

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

    def test_resolve_clang_prefers_clang_14_when_available(self) -> None:
        def fake_which(name: str) -> str | None:
            if name == "clang-14":
                return "/usr/bin/clang-14"
            return None

        args = argparse.Namespace(clang="", clang_mode="native")
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("agents.harness_generation.agent.Path.exists", return_value=False),
            mock.patch("agents.harness_generation.agent.shutil.which", side_effect=fake_which),
        ):
            clang = resolve_clang(args)

        self.assertEqual(clang, "/usr/bin/clang-14")

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

    def test_coverage_compact_summary_extracts_line_percent(self) -> None:
        summary = {
            "data": [
                {
                    "totals": {
                        "lines": {"count": 10, "covered": 7, "percent": 70.0},
                        "functions": {"count": 2, "covered": 1, "percent": 50.0},
                    }
                }
            ]
        }

        compact = coverage_compact_summary(summary)

        self.assertEqual(compact["line_percent"], 70.0)
        self.assertEqual(compact["metrics"]["lines"]["covered"], 7)

    def test_update_spec_coverage_writes_summary_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            coverage_dir = root / "coverage"
            coverage_dir.mkdir()
            log_path = coverage_dir / "coverage.log"
            summary_path = coverage_dir / "summary.json"
            report_path = coverage_dir / "report.md"
            log_path.write_text("ok\n", encoding="utf-8")
            summary_path.write_text("{}\n", encoding="utf-8")
            report_path.write_text("# Coverage\n", encoding="utf-8")
            payload = {"harness_spec": {"status": "run_succeeded", "diagnostics": []}}
            result = CoverageResult(
                status="success",
                message="coverage calculated",
                command=["llvm-cov", "export"],
                returncode=0,
                log_path=log_path,
                summary_path=summary_path,
                report_path=report_path,
                percent=42.5,
                details={"metrics": {"lines": {"covered": 17, "count": 40, "percent": 42.5}}},
            )

            update_spec_coverage(root, payload, result)

            spec = payload["harness_spec"]
            self.assertEqual(spec["coverage"]["status"], "success")
            self.assertEqual(spec["coverage"]["line_percent"], 42.5)
            self.assertEqual(spec["coverage"]["summary"], "coverage/summary.json")
            self.assertEqual(spec["coverage"]["report"], "coverage/report.md")

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
