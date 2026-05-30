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
    parser.add_argument("--report-json", required=True)
    parser.add_argument("--subsource", required=True)
    parser.add_argument("--calls", required=True)
    parser.add_argument("--params", required=True)
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
    report_path = Path(args.report_json)
    subsource_path = Path(args.subsource)
    calls_path = Path(args.calls)
    params_path = Path(args.params)
    missing = [str(path) for path in [report_path, subsource_path, calls_path, params_path] if not path.exists()]
    if missing:
        raise HarnessGenerationError("缺少知识库产物：" + ", ".join(missing))

    return {
        "function": args.function,
        "file": args.file,
        "repo": args.repo,
        "task_dir": args.task_dir,
        "report_json": _read_limited(report_path, MAX_REPORT_CHARS),
        "subsource": _read_limited(subsource_path, MAX_SOURCE_CHARS),
        "calls": _read_limited(calls_path, MAX_TEXT_CHARS),
        "params": _read_limited(params_path, MAX_TEXT_CHARS),
    }


def build_prompt(context: dict[str, str]) -> str:
    target = f"{context['file']}::{context['function']}" if context["file"] else context["function"]
    return f"""你是 AxF 的 Harness 生成 Agent。请基于下方 kRepo/AxF 知识库产物，为目标函数生成用户态 libFuzzer 驱动。

目标函数：{target}
源码根目录：{context['repo']}

要求：
1. 统一入口必须是 int LLVMFuzzerTestOneInput(const uint8_t *Data, size_t Size)。
2. 生成最小可读的用户态 C 代码，优先用 Data/Size 构造 buffer、长度、flags、枚举、地址结构、sk_buff 形态输入。
3. 如果目标严重依赖真实内核状态、硬件、并发或函数指针分派，请给出 unsupported 或 needs_manual_fixture，不要伪造成功。
4. 只能生成文件内容，不要修改 Linux 源码。
5. 生成代码会被本地 clang 立即编译验证；请尽量让 harness.c 和 mocks.c 可以仅依赖生成文件完成用户态编译。
6. 同时给出 Unix build.sh 和 Windows build.ps1。编译命令以 clang 和 libFuzzer sanitizer 为默认假设即可。
7. 输出必须是一个 JSON 对象，不要输出 Markdown。JSON schema：
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

--- report.json ---
{context['report_json']}

--- subsource bundle ---
{context['subsource']}

--- upstream calls ---
{context['calls']}

--- parameter constraints ---
{context['params']}
"""


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

    content = raw if body["stream"] else _choice_content(json.loads(raw))
    append_llm_transcript(
        transcript_path,
        interaction=interaction,
        model=model,
        messages=messages,
        assistant=content or raw,
    )
    log_llm_interaction("response", interaction, transcript_path)
    if not content:
        raise HarnessGenerationError("模型响应中没有 choices[0].message.content")
    try:
        return parse_model_json(content)
    except json.JSONDecodeError as exc:
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
    return url.rstrip("/") + "/chat/completions"


def read_chat_response(response: Any, *, streaming: bool) -> str:
    if not streaming:
        return response.read().decode("utf-8", errors="replace")
    return read_streaming_chat_response(response)


def read_streaming_chat_response(response: Any) -> str:
    parts: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").strip()
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
    return "".join(parts)


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
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise HarnessGenerationError("模型 JSON 顶层必须是对象")
    return value


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
    command = [
        clang,
        "-std=gnu11",
        "-fsanitize=fuzzer,address,undefined",
        "harness.c",
        "mocks.c",
        "-I.",
        "-o",
        binary_name,
    ]

    if not clang or (Path(clang).name == clang and shutil.which(clang) is None):
        output = f"clang executable not found: {clang or '<empty>'}\n"
        log_path.write_text(output, encoding="utf-8")
        return CompileResult(attempt, command, 127, output, log_path)

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
        log_path.write_text(output, encoding="utf-8")
        return CompileResult(attempt, command, completed.returncode, output, log_path)
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        output += f"\ncompile timed out after {args.compile_timeout} seconds\n"
        log_path.write_text(output, encoding="utf-8")
        return CompileResult(attempt, command, 124, output, log_path, timed_out=True)
    except OSError as exc:
        output = f"failed to start compiler: {exc}\n"
        log_path.write_text(output, encoding="utf-8")
        return CompileResult(attempt, command, 127, output, log_path)


def resolve_clang(args: argparse.Namespace) -> str:
    return args.clang or os.environ.get("CLANG") or "clang"


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
5. 这是轻量修复轮。只返回需要修改的文件，不要重复未修改文件。
6. 输出必须是一个 JSON 对象，不要输出 Markdown。最小 schema：
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
    text = first.get("text")
    return text if isinstance(text, str) else ""


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
clang -std=gnu11 -fsanitize=fuzzer,address,undefined harness.c mocks.c -I. -o fuzzer
"""


def default_build_ps1() -> str:
    return """$ErrorActionPreference = "Stop"
clang -std=gnu11 -fsanitize=fuzzer,address,undefined harness.c mocks.c -I. -o fuzzer.exe
"""


if __name__ == "__main__":
    raise SystemExit(main())
