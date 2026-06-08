from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = "glm-5.1"
DEFAULT_LLM_MODE = "api"
AI_CLI_TOOLS = ("nga", "opencode", "hac", "claude")
DEFAULT_OPENCODE_TOOL = "nga"
DEFAULT_CLI_EXECUTABLES = {
    "nga": "nga",
    "opencode": "opencode",
    "hac": "hac",
    "claude": "claude",
}
MAX_REPORT_CHARS = 50_000
MAX_SOURCE_CHARS = 90_000
MAX_TEXT_CHARS = 24_000
MAX_COMPILE_LOG_CHARS = 12_000
MAX_REPAIR_FILE_CHARS = 3_000
MAX_REPAIR_CONTEXT_CHARS = 2_000
DEFAULT_MAX_REPAIR_ROUNDS = 3
DEFAULT_RUN_SECONDS = 10
DEFAULT_MODEL_TIMEOUT = 300
DEFAULT_LLM_MAX_RETRIES = 2
OPENCODE_WORKSPACE_MESSAGE = (
    "你当前的 --dir 是本次 harness 生成的隔离工作区。必须先读取相对路径 context/prompt.md，"
    "该文件已经包含目标函数信息、输出 schema 和全部可用上下文。不要要求用户提供目标函数信息、"
    "kRepo/AxF 知识产物、知识库路径或规则库路径；缺少额外知识产物也必须先生成保守 harness。"
    "只输出一个可被 json.loads 解析的 JSON 对象，不要输出 Markdown 或解释。"
)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_local_env(PROJECT_ROOT / ".env.local")
    load_local_env(PROJECT_ROOT / ".env")

    try:
        result = generate_harness(args)
    except HarnessGenerationError as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 2
    except urllib.error.URLError as exc:
        print(f"请求模型失败：{exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"写入产物失败：{exc}", file=sys.stderr)
        return 4

    print(f"Harness 生成 Agent 已完成：{result['artifact']}")
    return 0


class HarnessGenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CompileResult:
    attempt: int
    command: list[str]
    returncode: int
    output: str
    log_path: Path
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


@dataclass(frozen=True)
class RunResult:
    command: list[str]
    returncode: int
    output: str
    log_path: Path
    seconds: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


@dataclass(frozen=True)
class CoverageResult:
    status: str
    message: str
    command: list[str]
    returncode: int
    log_path: Path
    summary_path: Path | None = None
    report_path: Path | None = None
    percent: float | None = None
    details: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status == "success"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Harness 生成 Agent：使用 LLM 基于知识库产物生成 libFuzzer 驱动")
    parser.add_argument("--function", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--report-json", default="")
    parser.add_argument("--subsource", default="")
    parser.add_argument("--calls", default="")
    parser.add_argument("--params", default="")
    parser.add_argument("--out", required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--file", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--chat-url", default="")
    parser.add_argument("--api-key-env", default="API_KEY")
    parser.add_argument("--llm-mode", choices=["api", "opencode"], default="")
    parser.add_argument("--opencode-tool", choices=AI_CLI_TOOLS, default="", help="AI CLI tool: nga, opencode, hac, or claude")
    parser.add_argument("--opencode-executable", default="", help="AI CLI executable path or name; Windows can use C:/tools/nga.cmd")
    parser.add_argument("--opencode-model", default="", help="model passed to AI CLI; empty uses CLI default")
    parser.add_argument("--timeout", type=int, default=DEFAULT_MODEL_TIMEOUT)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_LLM_MAX_RETRIES)
    parser.add_argument("--no-stream", action="store_true", help="disable streaming Chat Completions responses")
    parser.add_argument("--clang", default="", help="clang path used for local compile validation")
    parser.add_argument("--clang-mode", choices=["native", "wsl"], default="native", help="compile with native clang or WSL clang")
    parser.add_argument("--max-repair-rounds", type=int, default=DEFAULT_MAX_REPAIR_ROUNDS)
    parser.add_argument("--compile-timeout", type=int, default=60)
    parser.add_argument("--skip-compile", action="store_true", help="generate files without compile validation")
    parser.add_argument("--run-seconds", type=int, default=DEFAULT_RUN_SECONDS, help="run compiled fuzzer for this many seconds")
    parser.add_argument("--skip-run", action="store_true", help="skip the post-compile fuzzer run")
    parser.add_argument("--skip-coverage", action="store_true", help="skip post-run coverage calculation")
    return parser.parse_args(argv)


def generate_harness(args: argparse.Namespace) -> dict[str, str]:
    output_dir = Path(args.out).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = Path(args.artifact)
    transcript_path = output_dir / "llm_transcript.md"

    context = build_context(args)
    prompt = build_prompt(context)
    (output_dir / "llm_prompt.txt").write_text(prompt, encoding="utf-8")

    payload = request_harness_json(
        prompt=prompt,
        args=args,
        transcript_path=transcript_path,
        interaction="initial generation",
        record_only_harness=True,
    )
    payload = ensure_required_harness_payload(
        payload=payload,
        prompt=prompt,
        args=args,
        transcript_path=transcript_path,
        record_only_harness=True,
    )

    (output_dir / "llm_response.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    written = write_harness_files(output_dir, payload, context)
    payload, written = compile_and_repair(args, output_dir, payload, context, written)
    if transcript_path.exists():
        written = _merge_written(written, [transcript_path])
    write_artifact(artifact_path, output_dir, written, payload)
    return {"out": str(output_dir), "artifact": str(artifact_path)}


def build_context(args: argparse.Namespace) -> dict[str, str]:
    return {
        "function": args.function,
        "file": args.file,
        "repo": args.repo,
        "task_dir": args.task_dir,
        "report_json": read_optional_context(args.report_json, MAX_REPORT_CHARS),
        "subsource": read_optional_context(args.subsource, MAX_SOURCE_CHARS),
        "calls": read_optional_context(args.calls, MAX_TEXT_CHARS),
        "params": read_optional_context(args.params, MAX_TEXT_CHARS),
    }


def read_optional_context(value: str, limit: int) -> str:
    if not value:
        return ""
    path = Path(value)
    if not path.exists():
        raise HarnessGenerationError(f"指定的知识库产物不存在：{path}")
    return _read_limited(path, limit)


def build_prompt(context: dict[str, str]) -> str:
    target = f"{context['file']}::{context['function']}" if context["file"] else context["function"]
    context_sections = build_prompt_context_sections(context)
    return f"""你是 AxF 的 Harness 生成 Agent。请基于本 prompt 已经内联提供的目标函数信息和可选上下文，生成用户态 libFuzzer 驱动。

目标函数：{target}
源码根目录：{context['repo']}

要求：
1. 统一入口必须是 int LLVMFuzzerTestOneInput(const uint8_t *Data, size_t Size)。
2. 生成最小可读的用户态 C 代码，优先用 Data/Size 构造 buffer、长度、flags、枚举、地址结构、sk_buff 形态输入。
3. 目标函数信息已经在上方给出。本 prompt 中的 kRepo/AxF 知识产物只是可选参考，不是必须存在的外部知识库路径。不要回答“需要以下信息：目标函数信息、AxF/kRepo 知识产物”“没有找到 kRepo/AxF 知识产物”“缺少 harness 生成规则知识库路径”或要求用户补充路径；本 prompt 就是全部输入。
4. 即使没有额外 kRepo 知识产物，也要先尝试生成可编译 harness；不要因为上下文不足直接返回 unsupported。只有目标必须依赖不可模拟的真实硬件、真实内核并发语义或无法近似的函数指针分派时，才给出 unsupported 或 needs_manual_fixture。
5. 只能生成文件内容，不要修改 Linux 源码。
6. 生成代码会被本地 clang 立即编译验证；请尽量让 harness.c 和 mocks.c 可以仅依赖生成文件完成用户态编译。
7. 生成目录内必须包含目标函数 {context['function']} 的可链接定义。不能只写函数声明；如果 prompt 中提供了 subsource bundle，优先基于它写用户态适配实现；如果缺少源码上下文，请基于目标标识和通用 C/libFuzzer 经验写一个保守的用户态适配实现，并在 diagnostics/mock_rationale 中说明上下文受限。
8. 不要通过空实现目标函数、跳过目标调用、只调用 mock 函数来伪造编译成功。
9. dict.txt 只能包含短小 ASCII libFuzzer 字典项，最多 20 行；不要输出长二进制 blob，不要重复输出大量 \\x00。seed_hints 也必须短小。
10. 同时给出 Unix build.sh 和 Windows build.ps1。编译命令以 clang 和 libFuzzer sanitizer 为默认假设即可。
11. 除非 classification 是 unsupported 或 needs_manual_fixture 且原因不是“缺少上下文/知识产物”，files 必须包含 harness.c、mocks.h、mocks.c、build.sh、build.ps1。
12. 输出必须是一个 JSON 对象，不要输出 Markdown。JSON schema：
{{
  "classification": "byte_parser|skb_handler|sock_msg|net_device_state|unsupported|needs_manual_fixture",
  "unsupported_reason": "",
  "mock_rationale": "为什么这些 mock/fixture 足够或为什么不足",
  "seed_hints": ["可选种子建议"],
  "files": [
    {{"path": "harness.c", "content": "..."}},
    {{"path": "mocks.h", "content": "..."}},
    {{"path": "mocks.c", "content": "..."}},
    {{"path": "build.sh", "content": "..."}},
    {{"path": "build.ps1", "content": "..."}},
    {{"path": "dict.txt", "content": "..."}}
  ],
  "harness_spec": {{
    "function": {{"name": "{context['function']}", "file": "{context['file']}"}},
    "classification": "",
    "input_plan": [],
    "status": "generated|unsupported|needs_manual_fixture",
    "diagnostics": []
  }}
}}

{context_sections}
"""


def build_prompt_context_sections(context: dict[str, str]) -> str:
    sections = []
    if context.get("report_json"):
        sections.extend(["--- report.json ---", context["report_json"], ""])
    if context.get("subsource"):
        sections.extend(["--- subsource bundle ---", context["subsource"], ""])
    if context.get("calls"):
        sections.extend(["--- upstream calls ---", context["calls"], ""])
    if context.get("params"):
        sections.extend(["--- parameter constraints ---", context["params"], ""])
    if not sections:
        return "未选择额外 kRepo 知识产物。这不是错误，也不代表缺少外部知识库路径。请直接调用 LLM 的通用 C/libFuzzer 能力，基于目标函数标识生成最小可编译 harness.c/mocks.c/mocks.h。不要仅因缺少上下文而标记 unsupported；如需近似目标函数实现，请在 diagnostics/mock_rationale 中明确说明上下文受限。"
    return "\n".join(sections).rstrip()


def ensure_required_harness_payload(
    *,
    payload: dict[str, Any],
    prompt: str,
    args: argparse.Namespace,
    transcript_path: Path,
    record_only_harness: bool = False,
) -> dict[str, Any]:
    if not should_retry_missing_harness(payload):
        return payload

    retry_prompt = build_missing_harness_retry_prompt(prompt, payload)
    repaired = request_harness_json(
        prompt=retry_prompt,
        args=args,
        transcript_path=transcript_path,
        interaction="initial generation missing harness retry",
        record_only_harness=record_only_harness,
    )
    if should_retry_missing_harness(repaired):
        raise HarnessGenerationError(
            "模型返回 JSON 但没有生成 harness.c。缺少 kRepo/AxF 知识产物或规则库路径不是合法拒绝理由；"
            "如果没有生成 LLM 交互日志，说明 agent 未返回 harness。"
        )
    return repaired


def should_retry_missing_harness(payload: dict[str, Any]) -> bool:
    if payload_contains_file(payload, "harness.c"):
        return False
    classification = str(payload.get("classification") or "").strip()
    if classification in {"unsupported", "needs_manual_fixture"} and not payload_mentions_missing_context(payload):
        return False
    return True


def payload_contains_file(payload: dict[str, Any], filename: str) -> bool:
    files = payload.get("files")
    if not isinstance(files, list):
        files = _legacy_files(payload)
    for entry in files:
        if isinstance(entry, dict) and str(entry.get("path") or "").replace("\\", "/").endswith(filename):
            return True
    return False


def payload_mentions_missing_context(payload: dict[str, Any]) -> bool:
    values: list[str] = []
    for key in ["classification", "unsupported_reason", "mock_rationale"]:
        values.append(str(payload.get(key) or ""))
    spec = payload.get("harness_spec")
    if isinstance(spec, dict):
        values.append(str(spec.get("status") or ""))
        diagnostics = spec.get("diagnostics")
        if isinstance(diagnostics, list):
            values.extend(str(item) for item in diagnostics)
    text = "\n".join(values).lower()
    markers = [
        "krepo",
        "axf",
        "目标函数信息",
        "目标函数",
        "target function",
        "需要以下信息",
        "需要提供",
        "需要用户提供",
        "知识产物",
        "知识库",
        "规则库",
        "上下文",
        "context",
        "knowledge",
        "not found",
        "cannot find",
        "没有找到",
        "未找到",
        "缺少",
        "路径",
    ]
    return any(marker in text for marker in markers)


def build_missing_harness_retry_prompt(original_prompt: str, payload: dict[str, Any]) -> str:
    previous = json.dumps(payload, ensure_ascii=False)
    if len(previous) > 4000:
        previous = previous[:4000] + "...[truncated]"
    return (
        original_prompt
        + "\n\n上一轮模型返回了 JSON，但没有提供 files[].path == \"harness.c\"，或者把缺少目标函数信息、"
        "缺少 kRepo/AxF 知识产物、缺少 harness 生成规则库路径当成拒绝理由。这个理由无效：目标函数信息已经给出，"
        "本 prompt 已经包含全部可用输入，额外知识产物只是可选参考。"
        "\n请重新输出完整 JSON，除非目标确实依赖不可模拟的真实硬件、真实内核并发语义或无法近似的函数指针分派，否则必须包含 harness.c、mocks.h、mocks.c、build.sh、build.ps1。"
        "\n不要输出 Markdown，不要解释，不要要求用户提供目标函数信息或路径。"
        "\n\n上一轮 JSON 摘要：\n"
        + previous
    )


def request_harness_json(
    *,
    prompt: str,
    args: argparse.Namespace,
    transcript_path: Path | None = None,
    interaction: str = "",
    record_only_harness: bool = False,
) -> dict[str, Any]:
    mode = normalize_llm_mode(getattr(args, "llm_mode", "") or os.environ.get("LLM_MODE") or DEFAULT_LLM_MODE)
    if mode == "opencode":
        return request_harness_json_with_opencode(
            prompt=prompt,
            args=args,
            transcript_path=transcript_path,
            interaction=interaction,
            record_only_harness=record_only_harness,
        )
    return request_harness_json_with_api(
        prompt=prompt,
        args=args,
        transcript_path=transcript_path,
        interaction=interaction,
        record_only_harness=record_only_harness,
    )


def request_harness_json_with_api(
    *,
    prompt: str,
    args: argparse.Namespace,
    transcript_path: Path | None = None,
    interaction: str = "",
    record_only_harness: bool = False,
) -> dict[str, Any]:
    model = args.model or os.environ.get("MODEL") or DEFAULT_MODEL
    chat_url = normalize_chat_url(args.chat_url or _env_first("CHAT_COMPLETIONS_URL", "API_BASE_URL", "BASE_URL"))
    api_key_env = args.api_key_env or "API_KEY"

    if not chat_url:
        raise HarnessGenerationError("缺少 Chat Completions URL，请在 .env.local 设置 CHAT_COMPLETIONS_URL 或在前端填写")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise HarnessGenerationError(f"缺少 API key，请在环境变量或 .env.local 设置 {api_key_env}")

    messages = [
        {
            "role": "system",
            "content": "你是 AxF Harness 生成 Agent，只输出一个 JSON 对象，内容用于写入本地 fuzz harness 文件。",
        },
        {"role": "user", "content": prompt},
    ]
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "stream": not bool(getattr(args, "no_stream", False)),
    }
    max_retries = max(0, int(getattr(args, "max_retries", DEFAULT_LLM_MAX_RETRIES)))
    timeout = max(1, int(getattr(args, "timeout", DEFAULT_MODEL_TIMEOUT)))
    raw = ""
    for request_attempt in range(1, max_retries + 2):
        attempt_label = interaction_label(interaction, request_attempt, max_retries)
        log_llm_interaction("request", attempt_label, transcript_path)
        request = urllib.request.Request(
            chat_url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = read_chat_response(response, streaming=body["stream"])
            break
        except (TimeoutError, socket.timeout) as exc:
            error = f"模型请求超时（{timeout} 秒，第 {request_attempt}/{max_retries + 1} 次）：{exc}"
            record_llm_transcript(transcript_path, record_only_harness=record_only_harness, interaction=attempt_label, model=model, messages=messages, error=error)
            if request_attempt <= max_retries:
                log_llm_retry(error, request_attempt, max_retries)
                continue
            raise HarnessGenerationError(error) from exc
        except urllib.error.URLError as exc:
            error = f"请求模型失败（第 {request_attempt}/{max_retries + 1} 次）：{exc}"
            record_llm_transcript(transcript_path, record_only_harness=record_only_harness, interaction=attempt_label, model=model, messages=messages, error=error)
            if is_retryable_url_error(exc) and request_attempt <= max_retries:
                log_llm_retry(error, request_attempt, max_retries)
                continue
            raise HarnessGenerationError(error) from exc

    content = raw if body["stream"] else _choice_content_from_raw(raw)
    if body["stream"] and not content:
        fallback_body = dict(body)
        fallback_body["stream"] = False
        fallback_label = f"{interaction or 'llm'} non-stream fallback"
        log_llm_interaction("request", fallback_label, transcript_path)
        request = urllib.request.Request(
            chat_url,
            data=json.dumps(fallback_body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = read_chat_response(response, streaming=False)
            content = _choice_content_from_raw(raw)
        except (TimeoutError, socket.timeout) as exc:
            error = f"非流式模型请求超时（{timeout} 秒）：{exc}"
            record_llm_transcript(transcript_path, record_only_harness=record_only_harness, interaction=fallback_label, model=model, messages=messages, error=error)
            raise HarnessGenerationError(error) from exc
        except urllib.error.URLError as exc:
            error = f"非流式请求模型失败：{exc}"
            record_llm_transcript(transcript_path, record_only_harness=record_only_harness, interaction=fallback_label, model=model, messages=messages, error=error)
            raise HarnessGenerationError(error) from exc

    record_llm_transcript(
        transcript_path,
        record_only_harness=record_only_harness,
        interaction=interaction,
        model=model,
        messages=messages,
        assistant=content or raw,
    )
    log_llm_interaction("response", interaction, transcript_path)
    if not content:
        html_error = html_response_error(raw, chat_url)
        if html_error:
            raise HarnessGenerationError(html_error)
        raise HarnessGenerationError(
            "模型响应中没有 choices[0].message.content；响应预览："
            + response_preview(raw)
        )
    try:
        return parse_model_json(content)
    except json.JSONDecodeError as exc:
        retry = retry_invalid_json_response(
            chat_url=chat_url,
            api_key=api_key,
            model=model,
            original_prompt=prompt,
            base_body=body,
            timeout=timeout,
            transcript_path=transcript_path,
            interaction=interaction,
            error=exc,
            max_retries=max_retries,
            record_only_harness=record_only_harness,
        )
        if retry is not None:
            return retry
        raise HarnessGenerationError(f"模型没有返回合法 JSON：{exc}") from exc


def request_harness_json_with_opencode(
    *,
    prompt: str,
    args: argparse.Namespace,
    transcript_path: Path | None = None,
    interaction: str = "",
    record_only_harness: bool = False,
) -> dict[str, Any]:
    tool = normalize_ai_cli_tool(
        getattr(args, "opencode_tool", "")
        or os.environ.get("OPENCODE_TOOL")
        or "",
        getattr(args, "opencode_executable", "") or os.environ.get("OPENCODE_EXECUTABLE") or "",
    )
    executable = (
        getattr(args, "opencode_executable", "")
        or os.environ.get("OPENCODE_EXECUTABLE")
        or DEFAULT_CLI_EXECUTABLES[tool]
    )
    model = (
        getattr(args, "opencode_model", "")
        or os.environ.get("OPENCODE_MODEL")
        or getattr(args, "model", "")
        or os.environ.get("MODEL")
        or ""
    )
    timeout = max(1, int(getattr(args, "timeout", DEFAULT_MODEL_TIMEOUT)))
    max_retries = max(0, int(getattr(args, "max_retries", DEFAULT_LLM_MAX_RETRIES)))
    transcript_record_only_harness = record_only_harness and tool != "nga"
    messages = [
        {
            "role": "system",
            "content": "你是 AxF Harness 生成 Agent，只输出一个 JSON 对象，内容用于写入本地 fuzz harness 文件。",
        },
        {"role": "user", "content": prompt},
    ]

    current_prompt = prompt
    raw = ""
    for request_attempt in range(1, max_retries + 2):
        attempt_label = interaction_label(interaction or tool, request_attempt, max_retries)
        log_llm_interaction("request", attempt_label, transcript_path)
        workspace_dir = prepare_opencode_workspace(
            tool=tool,
            prompt=current_prompt,
            transcript_path=transcript_path,
            request_attempt=request_attempt,
        )
        try:
            command = build_opencode_command(
                tool=tool,
                executable=executable,
                repo=str(workspace_dir) if workspace_dir else args.repo,
                prompt=OPENCODE_WORKSPACE_MESSAGE if workspace_dir else current_prompt,
                model=model,
            )
        except FileNotFoundError as exc:
            error = str(exc)
            save_nga_interaction(
                transcript_path=transcript_path,
                attempt_label=attempt_label,
                request_attempt=request_attempt,
                model=model or f"{tool}-default",
                messages=messages,
                error=error,
                enabled=tool == "nga",
            )
            record_llm_transcript(transcript_path, record_only_harness=transcript_record_only_harness, interaction=attempt_label, model=model or f"{tool}-default", messages=messages, error=error)
            raise HarnessGenerationError(error) from exc
        try:
            completed = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = process_text(exc.stdout)
            stderr = process_text(exc.stderr)
            raw = "\n".join(part for part in [stdout, stderr] if part).strip()
            error = f"{tool} CLI 超时（{timeout} 秒，第 {request_attempt}/{max_retries + 1} 次）"
            save_nga_interaction(
                transcript_path=transcript_path,
                attempt_label=attempt_label,
                request_attempt=request_attempt,
                model=model or f"{tool}-default",
                messages=messages,
                command=command,
                stdout=stdout,
                stderr=stderr,
                raw=raw,
                error=error,
                enabled=tool == "nga",
            )
            record_llm_transcript(transcript_path, record_only_harness=transcript_record_only_harness, interaction=attempt_label, model=model or f"{tool}-default", messages=messages, assistant=raw, error=error)
            if request_attempt <= max_retries:
                log_llm_retry(error, request_attempt, max_retries)
                continue
            raise HarnessGenerationError(error) from exc
        except OSError as exc:
            error = (
                f"{tool} CLI 启动失败：{exc}。"
                "请检查模型设置中的 CLI executable；Windows 上建议填写完整路径，例如 C:/tools/nga.cmd。"
            )
            save_nga_interaction(
                transcript_path=transcript_path,
                attempt_label=attempt_label,
                request_attempt=request_attempt,
                model=model or f"{tool}-default",
                messages=messages,
                command=command,
                error=error,
                enabled=tool == "nga",
            )
            record_llm_transcript(transcript_path, record_only_harness=transcript_record_only_harness, interaction=attempt_label, model=model or f"{tool}-default", messages=messages, error=error)
            raise HarnessGenerationError(error) from exc

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        raw = "\n".join(part for part in [stdout, stderr] if part).strip()
        error_text = "" if completed.returncode == 0 else f"{tool} CLI 退出码：{completed.returncode}"
        save_nga_interaction(
            transcript_path=transcript_path,
            attempt_label=attempt_label,
            request_attempt=request_attempt,
            model=model or f"{tool}-default",
            messages=messages,
            command=command,
            stdout=stdout,
            stderr=stderr,
            raw=raw,
            returncode=completed.returncode,
            error=error_text,
            enabled=tool == "nga",
        )
        record_llm_transcript(
            transcript_path,
            record_only_harness=transcript_record_only_harness,
            interaction=attempt_label,
            model=model or f"{tool}-default",
            messages=messages,
            assistant=raw,
            error=error_text,
        )
        log_llm_interaction("response", attempt_label, transcript_path)
        if completed.returncode != 0:
            error = f"{tool} CLI 退出码：{completed.returncode}；输出预览：{response_preview(raw)}"
            if request_attempt <= max_retries:
                log_llm_retry(error, request_attempt, max_retries)
                continue
            raise HarnessGenerationError(error)
        try:
            return parse_model_json(raw)
        except json.JSONDecodeError as exc:
            if request_attempt <= max_retries:
                current_prompt = build_cli_json_retry_prompt(prompt, tool, exc)
                messages = [messages[0], {"role": "user", "content": current_prompt}]
                log_llm_retry(f"{tool} 输出不是合法 JSON：{exc}", request_attempt, max_retries)
                continue
            raise HarnessGenerationError(f"{tool} 没有返回合法 JSON：{exc}") from exc

    raise HarnessGenerationError(f"{tool} 未返回结果")


def build_cli_json_retry_prompt(original_prompt: str, tool: str, error: Exception) -> str:
    return (
        original_prompt
        + f"\n\n上一轮 {tool} 输出不是合法 JSON，解析错误：{error}。"
        + "\n请重新输出一个且仅一个完整 JSON 对象，必须以 { 开头并以 } 结尾，可被 Python json.loads 直接解析。"
        + "\n不要输出 Markdown、代码块、注释、解释、日志或任何 JSON 之外的字符。"
    )


def normalize_ai_cli_tool(tool: str, executable: str = "") -> str:
    value = (tool or "").strip().lower()
    if value in AI_CLI_TOOLS:
        return value
    inferred = Path(executable).name.lower() if executable else ""
    if inferred in AI_CLI_TOOLS:
        return inferred
    return DEFAULT_OPENCODE_TOOL


def build_opencode_command(
    *,
    tool: str = "",
    executable: str,
    repo: str,
    prompt: str,
    model: str = "",
) -> list[str]:
    selected_tool = normalize_ai_cli_tool(tool, executable)
    resolved_executable = resolve_cli_executable(selected_tool, executable)
    return build_cli_command(
        tool=selected_tool,
        executable=resolved_executable,
        repo=repo,
        prompt=prompt,
        model=model,
    )


def build_cli_command(
    *,
    tool: str,
    executable: str,
    repo: str,
    prompt: str,
    model: str = "",
) -> list[str]:
    if tool in {"nga", "opencode"}:
        repo_dir = resolve_repo_dir(repo)
        command = [executable, "run", "--dir", str(repo_dir)]
        if model:
            command.extend(["--model", model])
        command.append(prompt)
        return command

    if tool == "claude":
        command = [executable, "-p"]
        if model:
            command.extend(["--model", model])
        command.append(prompt)
        return command

    if tool == "hac":
        command = [executable]
        if model:
            command.extend(["--model", model])
        command.extend(["-p", prompt])
        return command

    raise HarnessGenerationError(f"未知 AI CLI 工具：{tool}")


def prepare_opencode_workspace(
    *,
    tool: str,
    prompt: str,
    transcript_path: Path | None,
    request_attempt: int,
) -> Path | None:
    if tool not in {"nga", "opencode"}:
        return None
    if transcript_path:
        workspace_dir = transcript_path.parent / "opencode_workspace"
    else:
        workspace_dir = PROJECT_ROOT / "workspace" / "opencode_workspace"
    context_dir = workspace_dir / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    (context_dir / f"prompt_request_{request_attempt}.md").write_text(prompt, encoding="utf-8")
    (workspace_dir / "TASK.md").write_text(OPENCODE_WORKSPACE_MESSAGE + "\n", encoding="utf-8")
    return workspace_dir


def save_nga_interaction(
    *,
    transcript_path: Path | None,
    attempt_label: str,
    request_attempt: int,
    model: str,
    messages: list[dict[str, str]],
    command: list[str] | None = None,
    stdout: str = "",
    stderr: str = "",
    raw: str = "",
    returncode: int | None = None,
    error: str = "",
    enabled: bool = False,
) -> None:
    if not enabled or transcript_path is None:
        return

    interaction_dir = transcript_path.parent / "nga_interactions" / interaction_dir_name(request_attempt, attempt_label)
    interaction_dir.mkdir(parents=True, exist_ok=True)
    system_message = "\n\n".join(str(message.get("content") or "") for message in messages if message.get("role") == "system")
    user_message = "\n\n".join(str(message.get("content") or "") for message in messages if message.get("role") == "user")
    (interaction_dir / "system.md").write_text(system_message, encoding="utf-8")
    (interaction_dir / "prompt.md").write_text(user_message, encoding="utf-8")
    (interaction_dir / "stdout.rawoutput").write_text(stdout or "", encoding="utf-8")
    (interaction_dir / "stderr.rawoutput").write_text(stderr or "", encoding="utf-8")
    (interaction_dir / "combined.rawoutput").write_text(raw or "\n".join(part for part in [stdout, stderr] if part).strip(), encoding="utf-8")
    metadata = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "interaction": attempt_label,
        "attempt": request_attempt,
        "model": model,
        "command": command or [],
        "returncode": returncode,
        "error": error,
    }
    (interaction_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def interaction_dir_name(request_attempt: int, attempt_label: str) -> str:
    safe = "".join(char if char.isalnum() else "_" for char in (attempt_label or "nga")).strip("_").lower()
    while "__" in safe:
        safe = safe.replace("__", "_")
    return f"{request_attempt:02d}_{safe or 'nga'}"


def process_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def resolve_cli_executable(tool: str, executable: str = "") -> str:
    selected_tool = normalize_ai_cli_tool(tool, executable)
    name = (executable or DEFAULT_CLI_EXECUTABLES[selected_tool]).strip()
    resolved = shutil.which(name)
    if resolved:
        return resolved
    if sys.platform != "win32":
        try:
            result = subprocess.run(
                ["bash", "-lc", f"command -v {shlex.quote(name)}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                path = result.stdout.strip()
                if path:
                    return path
        except Exception:
            pass
    raise FileNotFoundError(
        f"{selected_tool} executable '{name}' not found in PATH. "
        "请检查模型设置中的 CLI executable；Windows 上按 OpenDeepHole 的方式填写完整路径，例如 C:/tools/nga.cmd。"
    )


def resolve_repo_dir(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


def normalize_llm_mode(value: str) -> str:
    mode = (value or DEFAULT_LLM_MODE).strip().lower()
    if mode not in {"api", "opencode"}:
        raise HarnessGenerationError(f"未知 LLM 调用模式：{value}")
    return mode


def log_llm_interaction(kind: str, interaction: str, transcript_path: Path | None) -> None:
    if not transcript_path:
        return
    label = interaction or "llm"
    action = "请求已发送" if kind == "request" else "响应已返回"
    print(f"LLM 交互 [{label}] {action}", flush=True)


def log_llm_retry(error: str, request_attempt: int, max_retries: int) -> None:
    remaining = max_retries - request_attempt + 1
    print(f"{error}；准备重试，剩余 {remaining} 次", flush=True)


def retry_invalid_json_response(
    *,
    chat_url: str,
    api_key: str,
    model: str,
    original_prompt: str,
    base_body: dict[str, Any],
    timeout: int,
    transcript_path: Path | None,
    interaction: str,
    error: json.JSONDecodeError,
    max_retries: int,
    record_only_harness: bool = False,
) -> dict[str, Any] | None:
    retry_attempts = max(1, max_retries)
    last_error: json.JSONDecodeError | Exception = error
    for retry_attempt in range(1, retry_attempts + 1):
        retry_label = json_retry_label(interaction, retry_attempt, retry_attempts)
        retry_prompt = (
            original_prompt
            + "\n\n上一轮响应不是合法 JSON，解析错误："
            + str(last_error)
            + "\n请重新输出一个完整、可被 json.loads 解析的 JSON 对象。"
            + "\n额外限制：dict.txt 最多 20 行短 ASCII 字典项；不要输出长二进制 blob；不要输出大量重复的 \\x00；不要输出 Markdown。"
        )
        messages = [
            {
                "role": "system",
                "content": "你是 AxF Harness 生成 Agent，只输出一个 JSON 对象，内容用于写入本地 fuzz harness 文件。",
            },
            {"role": "user", "content": retry_prompt},
        ]
        body = dict(base_body)
        body["messages"] = messages
        body["stream"] = False

        log_llm_interaction("request", retry_label, transcript_path)
        request = urllib.request.Request(
            chat_url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = read_chat_response(response, streaming=False)
        except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
            last_error = exc
            record_llm_transcript(transcript_path, record_only_harness=record_only_harness, interaction=retry_label, model=model, messages=messages, error=str(exc))
            if retry_attempt < retry_attempts:
                log_retry_remaining(f"JSON 修复请求失败：{exc}", retry_attempt, retry_attempts)
                continue
            return None

        content = _choice_content_from_raw(raw)
        record_llm_transcript(transcript_path, record_only_harness=record_only_harness, interaction=retry_label, model=model, messages=messages, assistant=content or raw)
        log_llm_interaction("response", retry_label, transcript_path)
        if not content:
            last_error = HarnessGenerationError("JSON 修复响应中没有 choices[0].message.content")
            if retry_attempt < retry_attempts:
                log_retry_remaining(str(last_error), retry_attempt, retry_attempts)
                continue
            return None
        try:
            return parse_model_json(content)
        except json.JSONDecodeError as exc:
            last_error = exc
            if retry_attempt < retry_attempts:
                log_retry_remaining(f"JSON 修复响应仍不是合法 JSON：{exc}", retry_attempt, retry_attempts)
                continue
            return None
    return None


def interaction_label(interaction: str, request_attempt: int, max_retries: int) -> str:
    label = interaction or "llm"
    if max_retries <= 0:
        return label
    return f"{label} request {request_attempt}/{max_retries + 1}"


def json_retry_label(interaction: str, retry_attempt: int, retry_attempts: int) -> str:
    label = f"{interaction or 'llm'} JSON retry"
    if retry_attempts <= 1:
        return label
    return f"{label} {retry_attempt}/{retry_attempts}"


def log_retry_remaining(error: str, attempt: int, total_attempts: int) -> None:
    remaining = max(0, total_attempts - attempt)
    print(f"{error}；准备重试，剩余 {remaining} 次", flush=True)


def is_timeout_error(exc: urllib.error.URLError) -> bool:
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    return "timed out" in str(exc).lower()


def is_retryable_url_error(exc: urllib.error.URLError) -> bool:
    if is_timeout_error(exc):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (ConnectionResetError, ConnectionAbortedError, ssl.SSLError)):
        return True
    text = str(exc).lower()
    retryable_markers = [
        "unexpected_eof_while_reading",
        "eof occurred in violation of protocol",
        "ssl_error_syscall",
        "connection reset",
        "connection aborted",
        "temporarily unavailable",
    ]
    return any(marker in text for marker in retryable_markers)


def normalize_chat_url(value: str) -> str:
    url = (value or "").strip()
    if not url:
        return ""
    if url.rstrip("/").endswith("/chat/completions"):
        return url
    base = url.rstrip("/")
    if base.endswith("/api"):
        return base + "/v1/chat/completions"
    if base.endswith("/v1") or base.endswith("/api/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def read_chat_response(response: Any, *, streaming: bool) -> str:
    if not streaming:
        return response.read().decode("utf-8", errors="replace")
    return read_streaming_chat_response(response)


def read_streaming_chat_response(response: Any) -> str:
    parts: list[str] = []
    raw_lines: list[str] = []
    for raw_line in response:
        decoded = raw_line.decode("utf-8", errors="replace")
        raw_lines.append(decoded)
        line = decoded.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        for choice in chunk.get("choices", []):
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict) and delta.get("content") is not None:
                parts.append(str(delta.get("content")))
            message = choice.get("message")
            if isinstance(message, dict) and message.get("content") is not None:
                parts.append(str(message.get("content")))
    if parts:
        return "".join(parts)

    raw_text = "".join(raw_lines).strip()
    if not raw_text:
        return ""
    try:
        return _choice_content(json.loads(raw_text))
    except json.JSONDecodeError:
        return ""


def append_llm_transcript(
    path: Path | None,
    *,
    interaction: str,
    model: str,
    messages: list[dict[str, str]],
    assistant: str = "",
    error: str = "",
) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", encoding="utf-8") as handle:
        if not exists:
            handle.write("# LLM 交互日志\n\n")
            handle.write("API key 不会写入本日志；Chat Completions URL 只在本地配置中保存。\n\n")
        handle.write(f"## {interaction or 'llm'}\n\n")
        handle.write(f"- time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        handle.write(f"- model: {model}\n\n")
        for message in messages:
            role = str(message.get("role") or "message")
            content = str(message.get("content") or "")
            handle.write(f"### {role}\n\n")
            handle.write("````text\n")
            handle.write(content)
            if content and not content.endswith("\n"):
                handle.write("\n")
            handle.write("````\n\n")
        if assistant:
            handle.write("### assistant\n\n")
            handle.write("````text\n")
            handle.write(assistant)
            if not assistant.endswith("\n"):
                handle.write("\n")
            handle.write("````\n\n")
        if error:
            handle.write("### error\n\n")
            handle.write("````text\n")
            handle.write(error)
            if not error.endswith("\n"):
                handle.write("\n")
            handle.write("````\n\n")


def record_llm_transcript(
    path: Path | None,
    *,
    record_only_harness: bool,
    interaction: str,
    model: str,
    messages: list[dict[str, str]],
    assistant: str = "",
    error: str = "",
) -> None:
    if record_only_harness and not assistant_returns_harness(assistant):
        return
    append_llm_transcript(
        path,
        interaction=interaction,
        model=model,
        messages=messages,
        assistant=assistant,
        error=error,
    )


def assistant_returns_harness(assistant: str) -> bool:
    if not assistant.strip():
        return False
    try:
        payload = parse_model_json(assistant)
    except (HarnessGenerationError, json.JSONDecodeError):
        return False
    return payload_contains_file(payload, "harness.c")


def parse_model_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    value = load_model_json_text(text)
    if not isinstance(value, dict):
        raise HarnessGenerationError("模型 JSON 顶层必须是对象")
    return value


def load_model_json_text(text: str) -> Any:
    candidates = model_json_candidates(text)
    for balanced in extract_json_objects(text):
        candidates.extend(model_json_candidates(balanced))
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        sliced = text[start : end + 1]
        candidates.extend(model_json_candidates(sliced))

    last_error: json.JSONDecodeError | None = None
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise json.JSONDecodeError("empty JSON candidate", text, 0)


def extract_json_objects(text: str) -> list[str]:
    objects: list[str] = []
    start: int | None = None
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if start is None:
            if char == "{":
                start = index
                depth = 1
                in_string = False
                escape = False
            continue

        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                objects.append(text[start : index + 1])
                start = None
    return objects


def model_json_candidates(text: str) -> list[str]:
    variants: list[str] = []
    for single_quote_variant in [text, normalize_single_quoted_json_strings(text)]:
        for key_variant in [single_quote_variant, quote_bare_json_object_keys(single_quote_variant)]:
            for comma_variant in [key_variant, remove_trailing_json_commas(key_variant)]:
                variants.extend([comma_variant, escape_json_string_controls(comma_variant)])
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in variants:
        if candidate not in seen:
            deduped.append(candidate)
            seen.add(candidate)
    return deduped


def quote_bare_json_object_keys(text: str) -> str:
    result: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    escaped = False

    while index < length:
        char = text[index]
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue

        if char not in "{,":
            result.append(char)
            index += 1
            continue

        result.append(char)
        index += 1
        whitespace_start = index
        while index < length and text[index].isspace():
            result.append(text[index])
            index += 1
        key_start = index
        if index < length and text[index] in {"'", '"'}:
            quote = text[index]
            index += 1
            key_chars: list[str] = []
            key_escaped = False
            while index < length:
                current = text[index]
                if key_escaped:
                    key_chars.append(current)
                    key_escaped = False
                elif current == "\\":
                    key_escaped = True
                elif current == quote:
                    break
                else:
                    key_chars.append(current)
                index += 1
            if index < length and text[index] == quote:
                index += 1
                after_key = index
                while after_key < length and text[after_key].isspace():
                    after_key += 1
                if after_key < length and text[after_key] == ":":
                    result.append(json.dumps("".join(key_chars)))
                    continue
            result.append(text[key_start:index])
            continue

        if index < length and (text[index].isalpha() or text[index] in {"_", "$"}):
            index += 1
            while index < length and (text[index].isalnum() or text[index] in {"_", "$", "-"}):
                index += 1
            key = text[key_start:index]
            after_key = index
            while after_key < length and text[after_key].isspace():
                after_key += 1
            if after_key < length and text[after_key] == ":":
                result.append(f'"{key}"')
                continue
        if index < length:
            result.append(text[index])
            index += 1
        else:
            result.append(text[key_start:index])
        continue

    return "".join(result)


def normalize_single_quoted_json_strings(text: str) -> str:
    result: list[str] = []
    index = 0
    length = len(text)
    in_double_string = False
    escaped = False

    while index < length:
        char = text[index]
        if in_double_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_double_string = False
            index += 1
            continue

        if char == '"':
            in_double_string = True
            result.append(char)
            index += 1
            continue

        if char != "'":
            result.append(char)
            index += 1
            continue

        index += 1
        value_chars: list[str] = []
        single_escaped = False
        closed = False
        while index < length:
            current = text[index]
            if single_escaped:
                value_chars.append(current)
                single_escaped = False
            elif current == "\\":
                single_escaped = True
            elif current == "'":
                closed = True
                index += 1
                break
            else:
                value_chars.append(current)
            index += 1
        if closed:
            result.append(json.dumps("".join(value_chars)))
        else:
            result.append("'" + "".join(value_chars))
    return "".join(result)


def remove_trailing_json_commas(text: str) -> str:
    result: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    escaped = False

    while index < length:
        char = text[index]
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue

        if char != ",":
            result.append(char)
            index += 1
            continue

        lookahead = index + 1
        while lookahead < length and text[lookahead].isspace():
            lookahead += 1
        if lookahead < length and text[lookahead] in "}]":
            index += 1
            continue
        result.append(char)
        index += 1
    return "".join(result)


def escape_json_string_controls(text: str) -> str:
    result: list[str] = []
    in_string = False
    escaped = False

    for char in text:
        if not in_string:
            result.append(char)
            if char == '"':
                in_string = True
            continue

        if escaped:
            result.append(char)
            escaped = False
            continue

        if char == "\\":
            result.append(char)
            escaped = True
        elif char == '"':
            result.append(char)
            in_string = False
        elif char == "\n":
            result.append("\\n")
        elif char == "\r":
            result.append("\\r")
        elif char == "\t":
            result.append("\\t")
        elif ord(char) < 0x20:
            result.append(f"\\u{ord(char):04x}")
        else:
            result.append(char)

    return "".join(result)


def compile_and_repair(
    args: argparse.Namespace,
    output_dir: Path,
    payload: dict[str, Any],
    context: dict[str, str],
    written: list[Path],
) -> tuple[dict[str, Any], list[Path]]:
    max_repair_rounds = max(0, int(args.max_repair_rounds))
    results: list[CompileResult] = []

    if args.skip_compile:
        return finish_compile(output_dir, payload, written, [], "skipped", "compile skipped by --skip-compile")

    skip_reason = compile_skip_reason(output_dir, payload)
    if skip_reason:
        return finish_compile(output_dir, payload, written, [], "skipped", skip_reason)

    for attempt in range(1, max_repair_rounds + 2):
        result = compile_harness(output_dir, args, attempt)
        results.append(result)
        if result.ok:
            payload, written = finish_compile(output_dir, payload, written, results, "success", "compile succeeded")
            return run_after_compile(args, output_dir, payload, written)

        if attempt > max_repair_rounds:
            return finish_compile(output_dir, payload, written, results, "failed", "compile failed")

        repair_prompt = build_repair_prompt(context, output_dir, result, attempt, max_repair_rounds)
        (output_dir / f"llm_repair_prompt_{attempt}.txt").write_text(repair_prompt, encoding="utf-8")
        try:
            repaired_payload = request_harness_json(
                prompt=repair_prompt,
                args=args,
                transcript_path=output_dir / "llm_transcript.md",
                interaction=f"repair attempt {attempt}",
                record_only_harness=True,
            )
        except HarnessGenerationError as exc:
            return finish_compile(
                output_dir,
                payload,
                written,
                results,
                "failed",
                f"LLM repair request failed: {exc}",
            )
        (output_dir / f"llm_repair_response_{attempt}.json").write_text(
            json.dumps(repaired_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        payload = merge_repair_payload(payload, repaired_payload)
        written = write_harness_files(output_dir, payload, context)

    return payload, written


def run_after_compile(
    args: argparse.Namespace,
    output_dir: Path,
    payload: dict[str, Any],
    written: list[Path],
) -> tuple[dict[str, Any], list[Path]]:
    if args.skip_run:
        update_spec_run(output_dir, payload, None, "skipped", "run skipped by --skip-run")
        return payload, written

    run_result = run_harness(output_dir, args)
    status = "success" if run_result.ok else "timeout" if run_result.timed_out else "failed"
    message = f"{run_result.seconds} second run succeeded" if run_result.ok else "fuzzer run failed"
    update_spec_run(output_dir, payload, run_result, status, message)
    written = _merge_written(written, [run_result.log_path])
    if run_result.ok:
        coverage_result = calculate_coverage(output_dir, args)
        update_spec_coverage(output_dir, payload, coverage_result)
        coverage_paths = [coverage_result.log_path]
        if coverage_result.summary_path:
            coverage_paths.append(coverage_result.summary_path)
        if coverage_result.report_path:
            coverage_paths.append(coverage_result.report_path)
        written = _merge_written(written, coverage_paths)
    return payload, written


def finish_compile(
    output_dir: Path,
    payload: dict[str, Any],
    written: list[Path],
    results: list[CompileResult],
    status: str,
    message: str,
) -> tuple[dict[str, Any], list[Path]]:
    compile_log = write_compile_summary(output_dir, results, "" if results else message)
    update_spec_compile(output_dir, payload, status, results, message)
    return payload, _merge_written(written, [compile_log, *[item.log_path for item in results]])


def compile_skip_reason(output_dir: Path, payload: dict[str, Any]) -> str:
    spec = payload.get("harness_spec")
    status = str(spec.get("status") if isinstance(spec, dict) else payload.get("status") or "").strip()
    if not (output_dir / "harness.c").exists():
        if status in {"unsupported", "needs_manual_fixture"}:
            return f"compile skipped because harness status is {status} and harness.c was not generated"
        return "compile skipped because harness.c was not generated"
    return ""


def compile_harness(output_dir: Path, args: argparse.Namespace, attempt: int) -> CompileResult:
    mode = clang_mode(args)
    clang = resolve_clang(args)
    binary_name = "fuzzer" if mode == "wsl" else native_binary_name()
    log_path = output_dir / f"compile_attempt_{attempt}.log"
    commands = compile_commands_for_mode(output_dir, clang, binary_name, mode, log_path, attempt, args)
    if isinstance(commands, CompileResult):
        return commands
    command = commands[0]

    if mode == "native" and (not clang or (Path(clang).name == clang and shutil.which(clang) is None)):
        output = f"clang executable not found: {clang or '<empty>'}\n"
        log_path.write_text(output, encoding="utf-8")
        return CompileResult(attempt, command, 127, output, log_path)

    logs: list[str] = []
    last_result: CompileResult | None = None
    for index, command in enumerate(commands, start=1):
        label = f"variant {index}/{len(commands)}"
        logs.extend([f"## {label}", "$ " + " ".join(command), ""])
        try:
            completed = subprocess.run(
                command,
                cwd=output_dir,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=max(1, int(args.compile_timeout)),
                check=False,
            )
            output = completed.stdout or ""
            logs.extend([output.rstrip(), f"returncode: {completed.returncode}", ""])
            last_result = CompileResult(attempt, command, completed.returncode, output, log_path)
            if completed.returncode == 0:
                combined = "\n".join(logs).rstrip() + "\n"
                log_path.write_text(combined, encoding="utf-8")
                return CompileResult(attempt, command, 0, combined, log_path)
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") + (exc.stderr or "")
            output += f"\ncompile timed out after {args.compile_timeout} seconds\n"
            logs.extend([output.rstrip(), "returncode: 124", ""])
            combined = "\n".join(logs).rstrip() + "\n"
            log_path.write_text(combined, encoding="utf-8")
            return CompileResult(attempt, command, 124, combined, log_path, timed_out=True)
        except OSError as exc:
            output = f"failed to start compiler: {exc}\n"
            logs.extend([output.rstrip(), "returncode: 127", ""])
            last_result = CompileResult(attempt, command, 127, output, log_path)

    combined = "\n".join(logs).rstrip() + "\n"
    log_path.write_text(combined, encoding="utf-8")
    if last_result is None:
        return CompileResult(attempt, command, 127, combined, log_path)
    return CompileResult(attempt, last_result.command, last_result.returncode, combined, log_path)


def resolve_clang(args: argparse.Namespace) -> str:
    configured = getattr(args, "clang", "") or ""
    if configured:
        return configured
    if clang_mode(args) == "wsl":
        return "/usr/bin/clang"
    configured = os.environ.get("CLANG") or ""
    if configured:
        return configured
    if os.name != "nt":
        homebrew_clang = Path("/opt/homebrew/opt/llvm/bin/clang")
        if homebrew_clang.exists():
            return str(homebrew_clang)
    for candidate in ("clang-14", "clang"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return "clang"


def clang_mode(args: argparse.Namespace) -> str:
    mode = str(getattr(args, "clang_mode", "") or "native").strip().lower()
    return "wsl" if mode == "wsl" else "native"


def native_binary_name() -> str:
    return "fuzzer.exe" if os.name == "nt" else "fuzzer"


def compile_commands_for_mode(
    output_dir: Path,
    clang: str,
    binary_name: str,
    mode: str,
    log_path: Path,
    attempt: int,
    args: argparse.Namespace,
) -> list[list[str]] | CompileResult:
    native_commands = compile_command_variants(clang, binary_name)
    if mode != "wsl":
        return native_commands
    if shutil.which("wsl") is None:
        command = wsl_shell_command("<task-dir>", native_commands[0])
        output = "WSL executable not found. Use clang mode native, or install and enable WSL.\n"
        log_path.write_text(output, encoding="utf-8")
        return CompileResult(attempt, command, 127, output, log_path)
    wsl_dir, error = resolve_wsl_path(output_dir, min(10, max(1, int(getattr(args, "compile_timeout", 60)))))
    if error:
        command = ["wsl", "wslpath", "-a", str(output_dir)]
        log_path.write_text(error, encoding="utf-8")
        return CompileResult(attempt, command, 127, error, log_path)
    return [wsl_shell_command(wsl_dir, command) for command in native_commands]


def compile_command_variants(clang: str, binary_name: str) -> list[list[str]]:
    sanitizer_sets = [
        "fuzzer,address,undefined",
        "fuzzer,address",
        "fuzzer",
    ]
    return [
        [
            clang,
            "-std=gnu11",
            f"-fsanitize={sanitizers}",
            "harness.c",
            "mocks.c",
            "-I.",
            "-o",
            binary_name,
        ]
        for sanitizers in sanitizer_sets
    ]


def resolve_wsl_path(path: Path, timeout: int = 10) -> tuple[str, str]:
    command = ["wsl", "wslpath", "-a", str(path)]
    try:
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        return "", output + f"\nwslpath timed out after {timeout} seconds\n"
    except OSError as exc:
        return "", f"failed to run wslpath: {exc}\n"
    output = completed.stdout or ""
    if completed.returncode != 0:
        return "", output + f"\nwslpath failed with returncode {completed.returncode}\n"
    wsl_path = output.strip()
    if not wsl_path:
        return "", "wslpath returned an empty path\n"
    return wsl_path, ""


def wsl_shell_command(wsl_cwd: str, inner_command: list[str]) -> list[str]:
    shell_command = "cd " + shlex.quote(wsl_cwd) + " && " + shell_join(inner_command)
    return ["wsl", "sh", "-lc", shell_command]


def shell_join(command: list[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in command)


def run_harness(output_dir: Path, args: argparse.Namespace) -> RunResult:
    seconds = max(1, int(args.run_seconds))
    if clang_mode(args) == "wsl":
        return run_harness_wsl(output_dir, args, seconds)
    binary_name = native_binary_name()
    binary_path = output_dir / binary_name
    binary_cmd = f".\\{binary_name}" if os.name == "nt" else f"./{binary_name}"
    log_path = output_dir / "run.log"
    command = [binary_cmd, f"-max_total_time={seconds}", "-max_len=4096"]
    seed_dir = output_dir / "seed_corpus"
    if seed_dir.exists():
        command.append("seed_corpus")

    if not binary_path.exists():
        output = f"fuzzer binary not found: {binary_name}\n"
        write_run_log(log_path, command, 127, output, seconds, False)
        return RunResult(command, 127, output, log_path, seconds)

    env = os.environ.copy()
    env.setdefault("ASAN_OPTIONS", "detect_leaks=0")
    try:
        completed = subprocess.run(
            command,
            cwd=output_dir,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=seconds + 5,
            env=env,
            check=False,
        )
        output = completed.stdout or ""
        write_run_log(log_path, command, completed.returncode, output, seconds, False)
        return RunResult(command, completed.returncode, output, log_path, seconds)
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        output += f"\nfuzzer run timed out after {seconds + 5} seconds\n"
        write_run_log(log_path, command, 124, output, seconds, True)
        return RunResult(command, 124, output, log_path, seconds, timed_out=True)
    except OSError as exc:
        output = f"failed to start fuzzer: {exc}\n"
        write_run_log(log_path, command, 127, output, seconds, False)
        return RunResult(command, 127, output, log_path, seconds)


def run_harness_wsl(output_dir: Path, args: argparse.Namespace, seconds: int) -> RunResult:
    binary_name = "fuzzer"
    binary_path = output_dir / binary_name
    log_path = output_dir / "run.log"
    inner_command = ["env", "ASAN_OPTIONS=detect_leaks=0", f"./{binary_name}", f"-max_total_time={seconds}", "-max_len=4096"]
    seed_dir = output_dir / "seed_corpus"
    if seed_dir.exists():
        inner_command.append("seed_corpus")
    if not binary_path.exists():
        command = wsl_shell_command("<task-dir>", inner_command)
        output = f"fuzzer binary not found: {binary_name}\n"
        write_run_log(log_path, command, 127, output, seconds, False)
        return RunResult(command, 127, output, log_path, seconds)
    if shutil.which("wsl") is None:
        command = wsl_shell_command("<task-dir>", inner_command)
        output = "WSL executable not found. Use clang mode native, or install and enable WSL.\n"
        write_run_log(log_path, command, 127, output, seconds, False)
        return RunResult(command, 127, output, log_path, seconds)
    wsl_dir, error = resolve_wsl_path(output_dir, min(10, seconds + 5))
    if error:
        command = ["wsl", "wslpath", "-a", str(output_dir)]
        write_run_log(log_path, command, 127, error, seconds, False)
        return RunResult(command, 127, error, log_path, seconds)
    command = wsl_shell_command(wsl_dir, inner_command)
    try:
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=seconds + 5,
            check=False,
        )
        output = completed.stdout or ""
        write_run_log(log_path, command, completed.returncode, output, seconds, False)
        return RunResult(command, completed.returncode, output, log_path, seconds)
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        output += f"\nfuzzer run timed out after {seconds + 5} seconds\n"
        write_run_log(log_path, command, 124, output, seconds, True)
        return RunResult(command, 124, output, log_path, seconds, timed_out=True)
    except OSError as exc:
        output = f"failed to start fuzzer: {exc}\n"
        write_run_log(log_path, command, 127, output, seconds, False)
        return RunResult(command, 127, output, log_path, seconds)


def calculate_coverage(output_dir: Path, args: argparse.Namespace) -> CoverageResult:
    coverage_dir = output_dir / "coverage"
    coverage_dir.mkdir(parents=True, exist_ok=True)
    log_path = coverage_dir / "coverage.log"
    summary_path = coverage_dir / "summary.json"
    report_path = coverage_dir / "report.md"
    seconds = max(1, int(args.run_seconds))

    if getattr(args, "skip_coverage", False):
        result = CoverageResult("skipped", "coverage skipped by --skip-coverage", [], 0, log_path)
        write_coverage_log(result, ["coverage skipped by --skip-coverage"])
        return result
    if clang_mode(args) == "wsl":
        result = CoverageResult("skipped", "coverage is not supported in WSL mode yet", [], 0, log_path)
        write_coverage_log(result, ["coverage is not supported in WSL mode yet"])
        return result
    if os.name == "nt":
        result = CoverageResult("skipped", "coverage is not supported in native Windows mode yet", [], 0, log_path)
        write_coverage_log(result, ["coverage is not supported in native Windows mode yet"])
        return result

    clang = resolve_clang(args)
    profdata = resolve_llvm_tool("llvm-profdata", clang)
    cov = resolve_llvm_tool("llvm-cov", clang)
    if not profdata or not cov:
        missing = []
        if not profdata:
            missing.append("llvm-profdata")
        if not cov:
            missing.append("llvm-cov")
        message = "coverage skipped because tools are missing: " + ", ".join(missing)
        result = CoverageResult("skipped", message, [], 127, log_path)
        write_coverage_log(result, [message])
        return result

    binary_name = "fuzzer_coverage"
    compile_commands = coverage_compile_command_variants(clang, binary_name)
    logs: list[str] = ["# Coverage calculation", ""]
    compile_result: subprocess.CompletedProcess[str] | None = None
    last_command: list[str] = []
    for index, command in enumerate(compile_commands, start=1):
        last_command = command
        logs.extend([f"## compile coverage binary {index}/{len(compile_commands)}", "$ " + " ".join(command), ""])
        compile_result = run_coverage_subprocess(command, output_dir, max(1, int(args.compile_timeout)))
        logs.extend([(compile_result.stdout or "").rstrip(), f"returncode: {compile_result.returncode}", ""])
        if compile_result.returncode == 0:
            break
    if compile_result is None or compile_result.returncode != 0:
        message = "coverage binary compile failed"
        result = CoverageResult("failed", message, last_command, compile_result.returncode if compile_result else 127, log_path)
        write_coverage_log(result, logs)
        return result

    profraw_path = coverage_dir / "fuzzer.profraw"
    profdata_path = coverage_dir / "fuzzer.profdata"
    coverage_runs = max(1, min(1000, seconds * 100))
    run_command = [f"./{binary_name}", f"-runs={coverage_runs}", "-max_len=4096"]
    seed_dir = output_dir / "seed_corpus"
    if seed_dir.exists():
        run_command.append("seed_corpus")
    logs.extend(["## run coverage binary", "$ " + " ".join(run_command), ""])
    env = os.environ.copy()
    env.setdefault("ASAN_OPTIONS", "detect_leaks=0")
    env["LLVM_PROFILE_FILE"] = str(profraw_path)
    run_timeout = max(seconds + 15, seconds * 3, 30)
    run_result = run_coverage_subprocess(run_command, output_dir, run_timeout, env=env)
    logs.extend([(run_result.stdout or "").rstrip(), f"returncode: {run_result.returncode}", ""])
    if run_result.returncode != 0:
        message = "coverage run failed"
        result = CoverageResult("failed", message, run_command, run_result.returncode, log_path)
        write_coverage_log(result, logs)
        return result
    if not profraw_path.exists():
        message = "coverage run did not produce a profraw file"
        result = CoverageResult("failed", message, run_command, 1, log_path)
        write_coverage_log(result, logs)
        return result

    merge_command = [profdata, "merge", "-sparse", str(profraw_path), "-o", str(profdata_path)]
    logs.extend(["## merge profile", "$ " + " ".join(merge_command), ""])
    merge_result = run_coverage_subprocess(merge_command, output_dir, max(10, int(args.compile_timeout)))
    logs.extend([(merge_result.stdout or "").rstrip(), f"returncode: {merge_result.returncode}", ""])
    if merge_result.returncode != 0:
        message = "llvm-profdata merge failed"
        result = CoverageResult("failed", message, merge_command, merge_result.returncode, log_path)
        write_coverage_log(result, logs)
        return result

    export_command = [cov, "export", "--summary-only", f"./{binary_name}", f"-instr-profile={profdata_path}"]
    logs.extend(["## export coverage summary", "$ " + " ".join(export_command), ""])
    export_result = run_coverage_subprocess(export_command, output_dir, max(10, int(args.compile_timeout)))
    logs.extend([f"returncode: {export_result.returncode}", ""])
    if export_result.returncode != 0:
        logs.append((export_result.stdout or "").rstrip())
        message = "llvm-cov export failed"
        result = CoverageResult("failed", message, export_command, export_result.returncode, log_path)
        write_coverage_log(result, logs)
        return result

    try:
        summary = json.loads(export_result.stdout or "{}")
    except json.JSONDecodeError:
        message = "llvm-cov export returned invalid JSON"
        result = CoverageResult("failed", message, export_command, 1, log_path)
        write_coverage_log(result, logs)
        return result

    line_percent = coverage_line_percent(summary)
    compact_summary = coverage_compact_summary(summary)
    summary_path.write_text(json.dumps(compact_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(coverage_markdown_report(compact_summary), encoding="utf-8")
    message = "coverage calculated"
    result = CoverageResult(
        "success",
        message,
        export_command,
        0,
        log_path,
        summary_path=summary_path,
        report_path=report_path,
        percent=line_percent,
        details=compact_summary,
    )
    write_coverage_log(result, logs)
    return result


def resolve_llvm_tool(tool: str, clang: str) -> str:
    clang_name = Path(clang).name
    suffix_match = re.search(r"-(\d+)$", clang_name)
    names: list[str] = []
    if suffix_match:
        names.append(f"{tool}-{suffix_match.group(1)}")
    names.append(tool)
    candidates: list[str] = []
    if clang and Path(clang).is_absolute():
        clang_dir = Path(clang).parent
        candidates.extend(str(clang_dir / name) for name in names)
    candidates.extend(names)
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
        path = Path(candidate)
        if path.is_file():
            return str(path)
    return ""


def coverage_compile_command_variants(clang: str, binary_name: str) -> list[list[str]]:
    sanitizer_sets = [
        "fuzzer",
        "fuzzer,address",
        "fuzzer,address,undefined",
    ]
    return [
        [
            clang,
            "-std=gnu11",
            "-fprofile-instr-generate",
            "-fcoverage-mapping",
            f"-fsanitize={sanitizers}",
            "harness.c",
            "mocks.c",
            "-I.",
            "-o",
            binary_name,
        ]
        for sanitizers in sanitizer_sets
    ]


def run_coverage_subprocess(
    command: list[str],
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=max(1, timeout),
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        output += f"\ncoverage command timed out after {timeout} seconds\n"
        return subprocess.CompletedProcess(command, 124, output)
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, f"failed to start coverage command: {exc}\n")


def coverage_line_percent(summary: dict[str, Any]) -> float | None:
    totals = coverage_totals(summary)
    lines = totals.get("lines") if isinstance(totals.get("lines"), dict) else {}
    percent = lines.get("percent") if isinstance(lines, dict) else None
    try:
        return round(float(percent), 2)
    except (TypeError, ValueError):
        return None


def coverage_totals(summary: dict[str, Any]) -> dict[str, Any]:
    data = summary.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict) and isinstance(first.get("totals"), dict):
            return first["totals"]
    totals = summary.get("totals")
    return totals if isinstance(totals, dict) else {}


def coverage_compact_summary(summary: dict[str, Any]) -> dict[str, Any]:
    totals = coverage_totals(summary)
    metrics: dict[str, Any] = {}
    for name in ["lines", "functions", "regions", "branches"]:
        value = totals.get(name)
        if not isinstance(value, dict):
            continue
        metrics[name] = {
            key: value.get(key)
            for key in ["count", "covered", "notcovered", "percent"]
            if key in value
        }
    return {
        "status": "success",
        "line_percent": coverage_line_percent(summary),
        "metrics": metrics,
    }


def coverage_markdown_report(summary: dict[str, Any]) -> str:
    metrics = summary.get("metrics") if isinstance(summary.get("metrics"), dict) else {}
    lines = ["# Coverage Report", ""]
    line_percent = summary.get("line_percent")
    lines.extend([f"- Line coverage: {line_percent if line_percent is not None else 'unknown'}%", ""])
    lines.extend(["| Metric | Covered | Total | Percent |", "| --- | ---: | ---: | ---: |"])
    for name in ["lines", "functions", "regions", "branches"]:
        item = metrics.get(name)
        if not isinstance(item, dict):
            continue
        total = item.get("count", "")
        covered = item.get("covered", "")
        percent = item.get("percent", "")
        lines.append(f"| {name} | {covered} | {total} | {percent} |")
    lines.append("")
    return "\n".join(lines)


def write_coverage_log(result: CoverageResult, lines: list[str]) -> None:
    header = [
        f"status: {result.status}",
        f"message: {result.message}",
        f"returncode: {result.returncode}",
        "",
    ]
    result.log_path.write_text("\n".join(header + lines).rstrip() + "\n", encoding="utf-8")


def build_repair_prompt(
    context: dict[str, str],
    output_dir: Path,
    result: CompileResult,
    attempt: int,
    max_repair_rounds: int,
) -> str:
    current_files = read_current_harness_files(output_dir)
    diagnostics = result.output[-MAX_COMPILE_LOG_CHARS:]
    command = " ".join(result.command)
    params = limit_text(context.get("params", ""), MAX_REPAIR_CONTEXT_CHARS)
    calls = limit_text(context.get("calls", ""), MAX_REPAIR_CONTEXT_CHARS)
    return f"""你是 AxF 的 Harness 生成 Agent。上一次生成的 libFuzzer harness 编译失败，请根据编译诊断修复。

修复规则：
1. 只能修改生成目录中的文件：harness.c、mocks.h、mocks.c、build.sh、build.ps1、dict.txt、harness_spec.json。
2. 不要修改 Linux 源码，不要依赖真实内核构建环境。
3. 继续保持入口 int LLVMFuzzerTestOneInput(const uint8_t *Data, size_t Size)。
4. 优先补齐缺失类型、常量、函数声明和最小 mock；不要通过删除目标函数调用来让编译通过。
5. 如果诊断包含 undefined symbol 或 undefined reference to {context['function']}，必须在 harness.c 或 mocks.c 中提供目标函数的用户态适配定义，不能只补 prototype。
6. 不要通过空实现目标函数、删除目标调用、只调用 mock 函数来伪造编译成功。
7. 这是轻量修复轮。只返回需要修改的文件，不要重复未修改文件。
8. 输出必须是一个 JSON 对象，不要输出 Markdown。最小 schema：
{{
  "classification": "可选，沿用原分类时可省略",
  "mock_rationale": "简短说明本轮修复了什么",
  "files": [
    {{"path": "mocks.h", "content": "完整的新文件内容"}}
  ],
  "harness_spec": {{"diagnostics": ["可选，本轮修复说明"]}}
}}

目标函数：{context['file']}::{context['function']}
修复轮次：{attempt}/{max_repair_rounds}
编译命令：{command}
退出码：{result.returncode}

--- compile diagnostics ---
{diagnostics}

--- current generated files ---
{current_files}

--- parameter constraints ---
{params}

--- upstream calls ---
{calls}
"""


def read_current_harness_files(output_dir: Path) -> str:
    sections: list[str] = []
    for name in ["harness.c", "mocks.h", "mocks.c"]:
        path = output_dir / name
        if not path.exists():
            continue
        sections.extend([f"### {name}", "```", _read_limited(path, MAX_REPAIR_FILE_CHARS), "```", ""])
    return "\n".join(sections) or "No generated files found."


def merge_repair_payload(base: dict[str, Any], repair: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key in ["classification", "unsupported_reason", "mock_rationale", "seed_hints"]:
        if key in repair and repair[key] not in (None, ""):
            merged[key] = repair[key]

    base_files = files_by_path(base.get("files"))
    repair_files = files_by_path(repair.get("files"))
    base_files.update(repair_files)
    merged["files"] = list(base_files.values())

    base_spec = base.get("harness_spec")
    repair_spec = repair.get("harness_spec")
    if isinstance(base_spec, dict) or isinstance(repair_spec, dict):
        merged["harness_spec"] = merge_harness_spec(
            base_spec if isinstance(base_spec, dict) else {},
            repair_spec if isinstance(repair_spec, dict) else {},
        )
    return merged


def files_by_path(value: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(value, list):
        return result
    for entry in value:
        if not isinstance(entry, dict):
            continue
        rel_path = str(entry.get("path") or "").strip()
        if not rel_path or entry.get("content") is None:
            continue
        result[rel_path] = {"path": rel_path, "content": str(entry.get("content"))}
    return result


def merge_harness_spec(base: dict[str, Any], repair: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in repair.items():
        if key == "diagnostics" and isinstance(value, list):
            existing = merged.setdefault("diagnostics", [])
            if isinstance(existing, list):
                for item in value:
                    if item not in existing:
                        existing.append(item)
            continue
        if value not in (None, ""):
            merged[key] = value
    return merged


def update_spec_compile(
    output_dir: Path,
    payload: dict[str, Any],
    status: str,
    results: list[CompileResult],
    message: str,
) -> None:
    spec_path = output_dir / "harness_spec.json"
    spec = read_json_object(spec_path)
    if not spec:
        raw_spec = payload.get("harness_spec")
        spec = raw_spec if isinstance(raw_spec, dict) else {}
    attempts = [
        {
            "attempt": item.attempt,
            "command": item.command,
            "returncode": item.returncode,
            "timed_out": item.timed_out,
            "log": item.log_path.name,
        }
        for item in results
    ]
    spec["compile"] = {
        "status": status,
        "attempts": attempts,
        "message": message,
    }
    if status == "success":
        spec["status"] = "compiled"
    elif status == "failed":
        spec["status"] = "compile_failed"
    elif spec.get("status") not in {"unsupported", "needs_manual_fixture"}:
        spec["status"] = "generated"
    diagnostics = spec.setdefault("diagnostics", [])
    if isinstance(diagnostics, list) and message not in diagnostics:
        diagnostics.append(message)
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["harness_spec"] = spec


def update_spec_run(
    output_dir: Path,
    payload: dict[str, Any],
    result: RunResult | None,
    status: str,
    message: str,
) -> None:
    spec_path = output_dir / "harness_spec.json"
    spec = read_json_object(spec_path)
    if not spec:
        raw_spec = payload.get("harness_spec")
        spec = raw_spec if isinstance(raw_spec, dict) else {}
    run_info: dict[str, Any] = {"status": status, "message": message}
    if result is not None:
        run_info.update(
            {
                "seconds": result.seconds,
                "command": result.command,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "log": result.log_path.name,
            }
        )
    spec["run"] = run_info
    if status == "success":
        spec["status"] = "run_succeeded"
    elif status in {"failed", "timeout"} and spec.get("status") == "compiled":
        spec["status"] = "runtime_failed"
    diagnostics = spec.setdefault("diagnostics", [])
    if isinstance(diagnostics, list) and message not in diagnostics:
        diagnostics.append(message)
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["harness_spec"] = spec


def update_spec_coverage(output_dir: Path, payload: dict[str, Any], result: CoverageResult) -> None:
    spec_path = output_dir / "harness_spec.json"
    spec = read_json_object(spec_path)
    if not spec:
        raw_spec = payload.get("harness_spec")
        spec = raw_spec if isinstance(raw_spec, dict) else {}
    coverage_info: dict[str, Any] = {
        "status": result.status,
        "message": result.message,
        "returncode": result.returncode,
        "log": result.log_path.relative_to(output_dir).as_posix(),
    }
    if result.summary_path:
        coverage_info["summary"] = result.summary_path.relative_to(output_dir).as_posix()
    if result.report_path:
        coverage_info["report"] = result.report_path.relative_to(output_dir).as_posix()
    if result.percent is not None:
        coverage_info["line_percent"] = result.percent
    if result.details:
        coverage_info["metrics"] = result.details.get("metrics", {})
    spec["coverage"] = coverage_info
    diagnostics = spec.setdefault("diagnostics", [])
    if isinstance(diagnostics, list) and result.message not in diagnostics:
        diagnostics.append(result.message)
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["harness_spec"] = spec


def write_compile_summary(output_dir: Path, results: list[CompileResult], note: str = "") -> Path:
    path = output_dir / "compile.log"
    lines: list[str] = []
    if note:
        lines.extend([note, ""])
    if not results:
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return path
    for result in results:
        status = "success" if result.ok else "failed"
        lines.extend(
            [
                f"## attempt {result.attempt}: {status}",
                "$ " + " ".join(result.command),
                f"returncode: {result.returncode}",
                f"log: {result.log_path.name}",
                "",
                result.output.rstrip(),
                "",
            ]
        )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def write_run_log(
    path: Path,
    command: list[str],
    returncode: int,
    output: str,
    seconds: int,
    timed_out: bool,
) -> None:
    status = "timeout" if timed_out else "success" if returncode == 0 else "failed"
    lines = [
        f"## {seconds} second fuzzer run: {status}",
        "$ " + " ".join(command),
        f"seconds: {seconds}",
        f"returncode: {returncode}",
        "",
        output.rstrip(),
        "",
    ]
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _merge_written(existing: list[Path], extra: list[Path]) -> list[Path]:
    return sorted(set(existing + extra), key=lambda path: str(path))


def write_harness_files(output_dir: Path, payload: dict[str, Any], context: dict[str, str]) -> list[Path]:
    files = payload.get("files")
    if not isinstance(files, list):
        files = _legacy_files(payload)
    written: list[Path] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        rel_path = str(entry.get("path") or "").strip()
        content = entry.get("content")
        if not rel_path or content is None:
            continue
        target = _safe_output_path(output_dir, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
        if target.name in {"build.sh"}:
            target.chmod(target.stat().st_mode | stat.S_IXUSR)
        written.append(target)

    ensure_support_files(output_dir, payload, context, written)
    return sorted(set(written), key=lambda path: str(path))


def ensure_support_files(output_dir: Path, payload: dict[str, Any], context: dict[str, str], written: list[Path]) -> None:
    existing = {path.relative_to(output_dir).as_posix() for path in written}
    spec = payload.get("harness_spec")
    if not isinstance(spec, dict):
        spec = {}
    spec.setdefault("function", {"name": context["function"], "file": context["file"]})
    spec.setdefault("classification", payload.get("classification", "needs_manual_fixture"))
    spec.setdefault("status", "unsupported" if payload.get("unsupported_reason") else "generated")
    spec.setdefault("diagnostics", [])
    spec.setdefault("mock_rationale", payload.get("mock_rationale", ""))
    spec_path = output_dir / "harness_spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    written.append(spec_path)

    if "build.sh" not in existing:
        path = output_dir / "build.sh"
        path.write_text(default_build_sh(), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        written.append(path)
    if "build.ps1" not in existing:
        path = output_dir / "build.ps1"
        path.write_text(default_build_ps1(), encoding="utf-8")
        written.append(path)
    if "seed_corpus/README.txt" not in existing:
        seed_dir = output_dir / "seed_corpus"
        seed_dir.mkdir(exist_ok=True)
        hints = payload.get("seed_hints")
        text = "\n".join(str(item) for item in hints) if isinstance(hints, list) and hints else "Add seed files here.\n"
        path = seed_dir / "README.txt"
        path.write_text(text.rstrip() + "\n", encoding="utf-8")
        written.append(path)


def write_artifact(artifact_path: Path, output_dir: Path, files: list[Path], payload: dict[str, Any]) -> None:
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Harness 生成 Agent 产物",
        "",
        f"分类: {payload.get('classification', 'unknown')}",
        f"状态: {payload.get('harness_spec', {}).get('status', 'generated') if isinstance(payload.get('harness_spec'), dict) else 'generated'}",
        "",
        "## 文件",
    ]
    for path in files:
        rel = path.relative_to(output_dir).as_posix()
        lines.append(f"- {rel}")
    lines.append("")
    rationale = payload.get("mock_rationale")
    if rationale:
        lines.extend(["## Mock/Fixture 说明", "", str(rationale), ""])
    unsupported = payload.get("unsupported_reason")
    if unsupported:
        lines.extend(["## 未支持原因", "", str(unsupported), ""])
    lines.append("## 主要文件内容")
    for path in files:
        if path.suffix not in {".c", ".h", ".sh", ".ps1", ".json", ".txt", ".log"}:
            continue
        rel = path.relative_to(output_dir).as_posix()
        lines.extend(["", f"### {rel}", "", "```", _read_limited(path, 24_000), "```"])
    artifact_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def load_local_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped.removeprefix("export ").strip()
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def _choice_content(envelope: dict[str, Any]) -> str:
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
    delta = first.get("delta")
    if isinstance(delta, dict):
        content = delta.get("content")
        if isinstance(content, str):
            return content
    text = first.get("text")
    return text if isinstance(text, str) else ""


def _choice_content_from_raw(raw: str) -> str:
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(envelope, dict):
        return ""
    return _choice_content(envelope)


def response_preview(raw: str, limit: int = 800) -> str:
    preview = raw.strip().replace("\n", "\\n")
    if not preview:
        return "<empty>"
    if len(preview) > limit:
        return preview[:limit] + "...<truncated>"
    return preview


def html_response_error(raw: str, chat_url: str) -> str:
    text = raw.lstrip().lower()
    if not (text.startswith("<!doctype html") or text.startswith("<html") or "<title>new api</title>" in text):
        return ""
    return (
        "模型接口返回的是 HTML 网页，不是 Chat Completions JSON。"
        f"请检查 CHAT_COMPLETIONS_URL 或前端 Chat Completions URL，当前请求地址：{chat_url}。"
        "它必须是 API endpoint，例如 https://.../v1/chat/completions 或 https://.../api/v1/chat/completions，"
        "不能填 New API 管理页面首页。响应预览："
        + response_preview(raw)
    )


def _legacy_files(payload: dict[str, Any]) -> list[dict[str, str]]:
    mapping = {
        "harness_c": "harness.c",
        "mocks_h": "mocks.h",
        "mocks_c": "mocks.c",
        "build_sh": "build.sh",
        "build_ps1": "build.ps1",
        "dict_txt": "dict.txt",
    }
    return [
        {"path": path, "content": str(payload[key])}
        for key, path in mapping.items()
        if key in payload
    ]


def _safe_output_path(output_dir: Path, rel_path: str) -> Path:
    target = (output_dir / rel_path).resolve()
    try:
        target.relative_to(output_dir.resolve())
    except ValueError as exc:
        raise HarnessGenerationError(f"非法输出路径：{rel_path}") from exc
    return target


def _read_limited(path: Path, limit: int) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    return limit_text(text, limit)


def limit_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n/* truncated: {len(text) - limit} chars omitted */\n"


def _env_first(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def default_build_sh() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

CC="${CC:-/opt/homebrew/opt/llvm/bin/clang}"
if [ ! -x "$CC" ]; then
  CC="clang"
fi

"$CC" -std=gnu11 -fsanitize=fuzzer,address,undefined harness.c mocks.c -I. -o fuzzer || \
"$CC" -std=gnu11 -fsanitize=fuzzer,address harness.c mocks.c -I. -o fuzzer || \
"$CC" -std=gnu11 -fsanitize=fuzzer harness.c mocks.c -I. -o fuzzer
"""


def default_build_ps1() -> str:
    return """$ErrorActionPreference = "Stop"
$cc = if ($env:CLANG) { $env:CLANG } else { "clang" }
& $cc -std=gnu11 -fsanitize=fuzzer,address,undefined harness.c mocks.c -I. -o fuzzer.exe
if ($LASTEXITCODE -ne 0) {
  & $cc -std=gnu11 -fsanitize=fuzzer,address harness.c mocks.c -I. -o fuzzer.exe
}
if ($LASTEXITCODE -ne 0) {
  & $cc -std=gnu11 -fsanitize=fuzzer harness.c mocks.c -I. -o fuzzer.exe
}
exit $LASTEXITCODE
"""


if __name__ == "__main__":
    raise SystemExit(main())
