from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, TextIO

from frontend.server import (
    DEFAULT_MODEL_MAX_RETRIES,
    DEFAULT_MODEL_TIMEOUT,
    HARNESS_AGENT_ARTIFACT,
    PipelineStep,
    PROJECT_ROOT,
    _append_jsonl,
    _artifact_label,
    _completion_message,
    _harness_events_for_step,
    _harness_failure_message_for_step,
    _resolve_user_path,
    _step_action_label,
    build_steps,
    default_config,
)


TERMINAL_WORKSPACE = PROJECT_ROOT / "workspace" / "terminal" / "tasks"
ARTIFACT_OPTIONS = [
    ("report_md", "Markdown 报告"),
    ("report_json", "JSON 报告"),
    ("subsource", "下游源码包"),
    ("source", "源码分析包"),
    ("calls", "上层调用链"),
    ("params", "入参约束"),
    (HARNESS_AGENT_ARTIFACT, "生成 Fuzz Harness"),
]
HARNESS_CONTEXT_ARTIFACTS = {"report_json", "subsource", "calls", "params"}


class TerminalTaskRunner:
    def __init__(self, workspace: Path = TERMINAL_WORKSPACE, output: TextIO | None = None):
        self.workspace = Path(workspace)
        self.output = output or sys.stdout
        self.current_process: subprocess.Popen[str] | None = None

    def run(self, config: dict[str, Any]) -> int:
        task_id = uuid.uuid4().hex[:12]
        task_dir = self.workspace / task_id
        try:
            steps = build_steps(config, task_dir)
        except ValueError as exc:
            print(f"错误：{exc}", file=self.output, flush=True)
            return 2
        return self.run_steps(task_id, task_dir, config, steps)

    def submit_async(self, config: dict[str, Any]) -> int:
        task_id = uuid.uuid4().hex[:12]
        task_dir = self.workspace / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        try:
            build_steps(config, task_dir)
        except ValueError as exc:
            print(f"错误：{exc}", file=self.output, flush=True)
            return 2

        config_path = task_dir / "config.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        log_path = task_dir / "terminal.log"
        command = [
            sys.executable,
            "-m",
            "frontend.terminal",
            "worker",
            "--task-id",
            task_id,
            "--task-dir",
            str(task_dir),
            "--config",
            str(config_path),
        ]
        log_handle = None
        try:
            log_handle = log_path.open("a", encoding="utf-8")
            subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                close_fds=os.name != "nt",
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
        except OSError as exc:
            print(f"异步任务启动失败：{exc}", file=self.output, flush=True)
            return 127
        finally:
            if log_handle:
                log_handle.close()

        print(f"已提交异步任务：{task_id}", file=self.output, flush=True)
        print(f"产物目录：{task_dir}", file=self.output, flush=True)
        print(f"后台日志：{log_path}", file=self.output, flush=True)
        return 0

    def run_worker(self, task_id: str, task_dir: Path, config: dict[str, Any]) -> int:
        try:
            steps = build_steps(config, task_dir)
        except ValueError as exc:
            print(f"错误：{exc}", file=self.output, flush=True)
            return 2
        return self.run_steps(task_id, task_dir, config, steps)

    def run_steps(self, task_id: str, task_dir: Path, config: dict[str, Any], steps: list[PipelineStep]) -> int:
        task_dir.mkdir(parents=True, exist_ok=True)
        self._write_task_json(task_id, task_dir, config, steps)
        self._event(task_dir, "init", f"任务已开始：{config.get('function')}")

        try:
            for index, step in enumerate(steps, start=1):
                self._event(
                    task_dir,
                    step.name,
                    f"[{index}/{len(steps)}] {_step_action_label(step)}：{_artifact_label(step.artifact_name)}",
                    artifact=step.artifact_name,
                )
                self._log(task_dir, "$ " + " ".join(step.command), echo=True)
                returncode = self._run_step(task_dir, step)
                if returncode != 0:
                    self._event(task_dir, "failed", f"{_artifact_label(step.artifact_name)} 退出码：{returncode}")
                    print(f"失败：{_artifact_label(step.artifact_name)} 退出码 {returncode}", file=self.output, flush=True)
                    if step.capture_stdout:
                        print(f"输出文件：{step.artifact_path}", file=self.output, flush=True)
                    print(f"产物目录：{task_dir}", file=self.output, flush=True)
                    return returncode

                for event in _harness_events_for_step(step):
                    self._event(task_dir, event["phase"], event["message"], artifact=event.get("artifact"))
                failure = _harness_failure_message_for_step(step)
                if failure:
                    self._event(task_dir, "failed", failure, artifact=step.artifact_name)
                    print(f"失败：{failure}", file=self.output, flush=True)
                    print(f"产物目录：{task_dir}", file=self.output, flush=True)
                    return 1

                self._event(task_dir, step.name, f"{_artifact_label(step.artifact_name)} 已完成", artifact=step.artifact_name)

            message = _completion_message(task_dir)
            self._event(task_dir, "complete", message)
            print(f"完成：{message}", file=self.output, flush=True)
            print(f"产物目录：{task_dir}", file=self.output, flush=True)
            return 0
        except KeyboardInterrupt:
            if self.current_process and self.current_process.poll() is None:
                self.current_process.terminate()
            self._event(task_dir, "cancelled", "任务已停止")
            print("\n任务已停止", file=self.output, flush=True)
            print(f"产物目录：{task_dir}", file=self.output, flush=True)
            return 130
        finally:
            self.current_process = None

    def _run_step(self, task_dir: Path, step: PipelineStep) -> int:
        if step.reuse_from:
            try:
                step.artifact_path.parent.mkdir(parents=True, exist_ok=True)
                step.artifact_path.write_bytes(step.reuse_from.read_bytes())
            except OSError as exc:
                self._log(task_dir, f"复用失败：{exc}", echo=True)
                return 1
            return 0

        try:
            process = subprocess.Popen(
                step.command,
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self._log(task_dir, f"启动失败：{exc}", echo=True)
            return 127

        self.current_process = process
        assert process.stdout is not None
        output_handle = step.artifact_path.open("w", encoding="utf-8") if step.capture_stdout else None
        try:
            for line in process.stdout:
                if output_handle:
                    output_handle.write(line)
                else:
                    self._log(task_dir, line, echo=True)
            return process.wait()
        finally:
            if output_handle:
                output_handle.close()
            process.stdout.close()
            self.current_process = None

    def _write_task_json(self, task_id: str, task_dir: Path, config: dict[str, Any], steps: list[PipelineStep]) -> None:
        payload = {"id": task_id, "config": config, "steps": [step.to_json() for step in steps]}
        (task_dir / "task.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _event(self, task_dir: Path, phase: str, message: str, **extra: Any) -> None:
        event = {"ts": time.time(), "phase": phase, "message": message}
        event.update(extra)
        _append_jsonl(task_dir / "events.jsonl", event)
        timestamp = time.strftime("%H:%M:%S", time.localtime(event["ts"]))
        print(f"[{timestamp} | {phase_label(phase)}] {message}", file=self.output, flush=True)

    def _log(self, task_dir: Path, line: str, *, echo: bool = False) -> None:
        text = line.rstrip("\n")
        with (task_dir / "task.log").open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
        if echo:
            print(text, file=self.output, flush=True)


def phase_label(phase: str) -> str:
    labels = {
        "init": "初始化",
        "complete": "完成",
        "failed": "失败",
        "cancelled": "已停止",
        "harness_compile": "Harness 编译",
        "harness_run": "libFuzzer 试跑",
    }
    return labels.get(phase, _artifact_label(phase))


def parse_artifacts(value: str) -> list[str]:
    options = {name for name, _label in ARTIFACT_OPTIONS}
    by_number = {str(index): name for index, (name, _label) in enumerate(ARTIFACT_OPTIONS, start=1)}
    selected: list[str] = []
    for raw_item in value.replace(" ", ",").split(","):
        item = raw_item.strip()
        if not item:
            continue
        name = by_number.get(item, item)
        if name not in options:
            raise ValueError(f"未知产物选项：{item}")
        if name not in selected:
            selected.append(name)
    if not selected:
        raise ValueError("请至少选择一个 kRepo 产物或 AxF 后续流程")
    return selected


def terminal_default_config() -> dict[str, Any]:
    return default_config()


def config_from_args(args: argparse.Namespace, *, input_func=input, output: TextIO | None = None) -> dict[str, Any]:
    stream = output or sys.stdout
    if args.non_interactive:
        missing = [name for name in ["repo", "function", "artifacts"] if not getattr(args, name)]
        if missing:
            raise ValueError("非交互模式缺少必填参数：" + ", ".join("--" + name.replace("_", "-") for name in missing))
        config = terminal_default_config()
        apply_args_to_config(config, args)
        config["artifacts"] = parse_artifacts(args.artifacts)
        return config

    config = terminal_default_config()
    apply_args_to_config(config, args)
    print("AxF Terminal Pipeline", file=stream)
    print("直接回车使用括号中的默认值。", file=stream)
    config["repo"] = prompt_text("源码根目录", str(args.repo or config["repo"]), input_func, stream, required=True)
    config["db"] = prompt_text("BROWSE.VC.DB（留空自动查找）", str(args.db or config["db"]), input_func, stream)
    config["knowledge_dir"] = prompt_text(
        "复用知识库目录（留空则重新抽取）",
        str(args.knowledge_dir or config["knowledge_dir"]),
        input_func,
        stream,
    )
    config["function"] = prompt_text("函数名", str(args.function or config["function"]), input_func, stream, required=True)
    config["file"] = prompt_text("文件过滤", str(args.file or config["file"]), input_func, stream)
    config["model"] = prompt_text("模型", str(args.model or config["model"]), input_func, stream)
    config["chat_url"] = prompt_text("Chat Completions URL", str(args.chat_url or config["chat_url"]), input_func, stream)
    config["api_key_env"] = prompt_text("API Key 环境变量", str(args.api_key_env or config["api_key_env"]), input_func, stream)
    config["model_timeout"] = prompt_int("模型超时秒数", args.model_timeout or config["model_timeout"], input_func, stream)
    config["model_max_retries"] = prompt_int("模型重试次数", args.model_max_retries or config["model_max_retries"], input_func, stream)
    config["clang"] = prompt_text("Clang 路径", str(args.clang or config["clang"]), input_func, stream)
    config["max_repair_rounds"] = prompt_int("最大修复轮数", args.max_repair_rounds or config["max_repair_rounds"], input_func, stream)
    config["compile_timeout"] = prompt_int("编译超时秒数", args.compile_timeout or config["compile_timeout"], input_func, stream)
    config["max_deps"] = prompt_int("依赖上限", args.max_deps or config["max_deps"], input_func, stream)
    config["max_snippet_lines"] = prompt_int("片段行数", args.max_snippet_lines or config["max_snippet_lines"], input_func, stream)
    config["max_depth"] = prompt_int("下游深度", args.max_depth or config["max_depth"], input_func, stream)
    config["max_functions"] = prompt_int("函数上限", args.max_functions or config["max_functions"], input_func, stream)
    config["call_depth"] = prompt_int("调用链深度", args.call_depth or config["call_depth"], input_func, stream)
    config["max_candidates"] = prompt_int("同名候选上限", args.max_candidates or config["max_candidates"], input_func, stream)
    config["artifacts"] = prompt_artifacts(args.artifacts, config["artifacts"], input_func, stream)
    return config


def apply_args_to_config(config: dict[str, Any], args: argparse.Namespace) -> None:
    mapping = {
        "repo": args.repo,
        "db": args.db,
        "knowledge_dir": args.knowledge_dir,
        "function": args.function,
        "file": args.file,
        "model": args.model,
        "chat_url": args.chat_url,
        "api_key_env": args.api_key_env,
        "model_timeout": args.model_timeout,
        "model_max_retries": args.model_max_retries,
        "clang": args.clang,
        "max_repair_rounds": args.max_repair_rounds,
        "compile_timeout": args.compile_timeout,
        "max_deps": args.max_deps,
        "max_snippet_lines": args.max_snippet_lines,
        "max_depth": args.max_depth,
        "max_functions": args.max_functions,
        "call_depth": args.call_depth,
        "max_candidates": args.max_candidates,
    }
    for key, value in mapping.items():
        if value not in (None, ""):
            config[key] = value


def prompt_text(label: str, default: str, input_func: Any, output: TextIO, *, required: bool = False) -> str:
    while True:
        value = input_func(f"{label} [{default}]: ").strip()
        result = value if value else default
        if result or not required:
            return result
        print(f"{label} 不能为空。", file=output)


def prompt_int(label: str, default: int, input_func: Any, output: TextIO) -> int:
    while True:
        value = input_func(f"{label} [{default}]: ").strip()
        if not value:
            return int(default)
        try:
            return int(value)
        except ValueError:
            print(f"{label} 必须是整数。", file=output)


def prompt_artifacts(value: str | None, default: list[str], input_func: Any, output: TextIO) -> list[str]:
    print("\n产物选项：", file=output)
    for index, (name, label) in enumerate(ARTIFACT_OPTIONS, start=1):
        prompt_note = ""
        if name in HARNESS_CONTEXT_ARTIFACTS:
            prompt_note = "；勾选 Harness 时会加入 prompt"
        elif name in {"report_md", "source"}:
            prompt_note = "；不加入 Harness prompt"
        print(f"  {index}. {name} - {label}{prompt_note}", file=output)
    current = parse_artifacts(value) if value else list(default)
    default_text = ",".join(str(index) for index, (name, _label) in enumerate(ARTIFACT_OPTIONS, start=1) if name in current)
    while True:
        raw = input_func(f"选择产物编号或 id，逗号分隔 [{default_text}]: ").strip()
        try:
            return parse_artifacts(raw) if raw else current
        except ValueError as exc:
            print(f"错误：{exc}", file=output)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AxF terminal pipeline")
    subparsers = parser.add_subparsers(dest="command")
    run_parser = subparsers.add_parser("run", description="交互或脚本化运行 AxF pipeline")
    add_run_arguments(run_parser)
    worker_parser = subparsers.add_parser("worker", description=argparse.SUPPRESS)
    worker_parser.add_argument("--task-id", required=True)
    worker_parser.add_argument("--task-dir", required=True)
    worker_parser.add_argument("--config", required=True)
    return parser.parse_args(argv)


def add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo")
    parser.add_argument("--db")
    parser.add_argument("--knowledge-dir", help="复用已有知识库产物目录")
    parser.add_argument("--function")
    parser.add_argument("--file")
    parser.add_argument("--artifacts", help="逗号分隔的产物 id 或编号")
    parser.add_argument("--model")
    parser.add_argument("--chat-url")
    parser.add_argument("--api-key-env")
    parser.add_argument("--model-timeout", type=int)
    parser.add_argument("--model-max-retries", type=int)
    parser.add_argument("--clang")
    parser.add_argument("--max-repair-rounds", type=int)
    parser.add_argument("--compile-timeout", type=int)
    parser.add_argument("--max-deps", type=int)
    parser.add_argument("--max-snippet-lines", type=int)
    parser.add_argument("--max-depth", type=int)
    parser.add_argument("--max-functions", type=int)
    parser.add_argument("--call-depth", type=int)
    parser.add_argument("--max-candidates", type=int)
    parser.add_argument("--workspace", default=str(TERMINAL_WORKSPACE))
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--async", dest="async_run", action="store_true", help="后台提交任务后立即返回")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "worker":
        task_dir = _resolve_user_path(args.task_dir)
        config_path = _resolve_user_path(args.config)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        return TerminalTaskRunner(output=sys.stdout).run_worker(args.task_id, task_dir, config)
    if args.command != "run":
        print("用法：python -m frontend.terminal run [options]", file=sys.stderr)
        return 2
    try:
        config = config_from_args(args)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    runner = TerminalTaskRunner(Path(args.workspace))
    if args.async_run:
        return runner.submit_async(config)
    return runner.run(config)


if __name__ == "__main__":
    raise SystemExit(main())
