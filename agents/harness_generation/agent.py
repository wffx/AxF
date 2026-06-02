from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
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
    parser.add_argument("--timeout", type=int, default=DEFAULT_MODEL_TIMEOUT)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_LLM_MAX_RETRIES)
    parser.add_argument("--no-stream", action="store_true", help="disable streaming Chat Completions responses")
    parser.add_argument("--clang", default="", help="clang path used for local compile validation")
    parser.add_argument("--max-repair-rounds", type=int, default=DEFAULT_MAX_REPAIR_ROUNDS)
    parser.add_argument("--compile-timeout", type=int, default=60)
    parser.add_argument("--skip-compile", action="store_true", help="generate files without compile validation")
    parser.add_argument("--run-seconds", type=int, default=DEFAULT_RUN_SECONDS, help="run compiled fuzzer for this many seconds")
    parser.add_argument("--skip-run", action="store_true", help="skip the post-compile fuzzer run")
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
    return f"""你是 AxF 的 Harness 生成 Agent。请基于目标函数信息和用户选择加入 prompt 的 kRepo/AxF 知识产物，生成用户态 libFuzzer 驱动。

目标函数：{target}
源码根目录：{context['repo']}

要求：
1. 统一入口必须是 int LLVMFuzzerTestOneInput(const uint8_t *Data, size_t Size)。
2. 生成最小可读的用户态 C 代码，优先用 Data/Size 构造 buffer、长度、flags、枚举、地址结构、sk_buff 形态输入。
3. 如果目标严重依赖真实内核状态、硬件、并发或函数指针分派，请给出 unsupported 或 needs_manual_fixture，不要伪造成功。
4. 只能生成文件内容，不要修改 Linux 源码。
5. 生成代码会被本地 clang 立即编译验证；请尽量让 harness.c 和 mocks.c 可以仅依赖生成文件完成用户态编译。
6. 生成目录内必须包含目标函数 {context['function']} 的可链接定义。不能只写函数声明；如果 prompt 中提供了 subsource bundle，优先基于它写用户态适配实现；如果缺少足够源码上下文，请标记 unsupported/needs_manual_fixture。
7. 不要通过空实现目标函数、跳过目标调用、只调用 mock 函数来伪造编译成功。
8. dict.txt 只能包含短小 ASCII libFuzzer 字典项，最多 20 行；不要输出长二进制 blob，不要重复输出大量 \\x00。seed_hints 也必须短小。
9. 同时给出 Unix build.sh 和 Windows build.ps1。编译命令以 clang 和 libFuzzer sanitizer 为默认假设即可。
10. 输出必须是一个 JSON 对象，不要输出 Markdown。JSON schema：
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
        return "未选择额外 kRepo 知识产物。请仅基于目标函数标识和通用 libFuzzer/C 经验生成；信息不足时标记 unsupported 或 needs_manual_fixture。"
    return "\n".join(sections).rstrip()


def request_harness_json(
    *,
    prompt: str,
    args: argparse.Namespace,
    transcript_path: Path | None = None,
    interaction: str = "",
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
            append_llm_transcript(transcript_path, interaction=attempt_label, model=model, messages=messages, error=error)
            if request_attempt <= max_retries:
                log_llm_retry(error, request_attempt, max_retries)
                continue
            raise HarnessGenerationError(error) from exc
        except urllib.error.URLError as exc:
            error = f"请求模型失败（第 {request_attempt}/{max_retries + 1} 次）：{exc}"
            append_llm_transcript(transcript_path, interaction=attempt_label, model=model, messages=messages, error=error)
            if is_timeout_error(exc) and request_attempt <= max_retries:
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
            append_llm_transcript(transcript_path, interaction=fallback_label, model=model, messages=messages, error=error)
            raise HarnessGenerationError(error) from exc
        except urllib.error.URLError as exc:
            error = f"非流式请求模型失败：{exc}"
            append_llm_transcript(transcript_path, interaction=fallback_label, model=model, messages=messages, error=error)
            raise HarnessGenerationError(error) from exc

    append_llm_transcript(
        transcript_path,
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
        )
        if retry is not None:
            return retry
        raise HarnessGenerationError(f"模型没有返回合法 JSON：{exc}") from exc


def log_llm_interaction(kind: str, interaction: str, transcript_path: Path | None) -> None:
    if not transcript_path:
        return
    label = interaction or "llm"
    action = "请求" if kind == "request" else "响应"
    print(f"LLM 交互 [{label}] {action}已记录：{transcript_path}", flush=True)


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
) -> dict[str, Any] | None:
    retry_label = f"{interaction or 'llm'} JSON retry"
    retry_prompt = (
        original_prompt
        + "\n\n上一轮响应不是合法 JSON，解析错误："
        + str(error)
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
        append_llm_transcript(transcript_path, interaction=retry_label, model=model, messages=messages, error=str(exc))
        return None

    content = _choice_content_from_raw(raw)
    append_llm_transcript(transcript_path, interaction=retry_label, model=model, messages=messages, assistant=content or raw)
    log_llm_interaction("response", retry_label, transcript_path)
    if not content:
        return None
    try:
        return parse_model_json(content)
    except json.JSONDecodeError:
        return None


def interaction_label(interaction: str, request_attempt: int, max_retries: int) -> str:
    label = interaction or "llm"
    if max_retries <= 0:
        return label
    return f"{label} request {request_attempt}/{max_retries + 1}"


def is_timeout_error(exc: urllib.error.URLError) -> bool:
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    return "timed out" in str(exc).lower()


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
    candidates = [text, escape_json_string_controls(text)]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        sliced = text[start : end + 1]
        candidates.extend([sliced, escape_json_string_controls(sliced)])

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
    return payload, _merge_written(written, [run_result.log_path])


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
    if status in {"unsupported", "needs_manual_fixture"}:
        return f"compile skipped because harness status is {status}"
    if not (output_dir / "harness.c").exists():
        return "compile skipped because harness.c was not generated"
    return ""


def compile_harness(output_dir: Path, args: argparse.Namespace, attempt: int) -> CompileResult:
    clang = resolve_clang(args)
    binary_name = "fuzzer.exe" if os.name == "nt" else "fuzzer"
    log_path = output_dir / f"compile_attempt_{attempt}.log"
    commands = compile_command_variants(clang, binary_name)
    command = commands[0]

    if not clang or (Path(clang).name == clang and shutil.which(clang) is None):
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
    configured = getattr(args, "clang", "") or os.environ.get("CLANG") or ""
    if configured:
        return configured
    if os.name != "nt":
        homebrew_clang = Path("/opt/homebrew/opt/llvm/bin/clang")
        if homebrew_clang.exists():
            return str(homebrew_clang)
    return "clang"


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


def run_harness(output_dir: Path, args: argparse.Namespace) -> RunResult:
    seconds = max(1, int(args.run_seconds))
    binary_name = "fuzzer.exe" if os.name == "nt" else "fuzzer"
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
