from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).with_name("static")
DEFAULT_WORKSPACE = PROJECT_ROOT / "workspace" / "web" / "tasks"
HARNESS_AGENT_ARTIFACT = "harness_generation_agent"
DEFAULT_MODEL_TIMEOUT = 300
DEFAULT_MODEL_MAX_RETRIES = 2


@dataclass(frozen=True)
class PipelineStep:
    name: str
    command: list[str]
    artifact_name: str
    artifact_path: Path
    capture_stdout: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "artifact_name": self.artifact_name,
            "artifact_path": str(self.artifact_path),
            "capture_stdout": self.capture_stdout,
        }


@dataclass
class Task:
    id: str
    config: dict[str, Any]
    task_dir: Path
    status: str = "queued"
    returncode: int | None = None
    log: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    process: subprocess.Popen[str] | None = None
    cancel_requested: bool = False
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "config": self.config,
            "task_dir": str(self.task_dir),
            "status": self.status,
            "returncode": self.returncode,
            "error": self.error,
            "log": self.log,
            "events": self.events,
            "artifacts": self.artifacts,
            "harness": _harness_summary(self.task_dir),
        }


class TaskStore:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}

    def list(self) -> list[Task]:
        with self._lock:
            return list(reversed(self._tasks.values()))

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def create(self, config: dict[str, Any]) -> Task:
        task_id = uuid.uuid4().hex[:12]
        task_dir = self.workspace / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        task = Task(id=task_id, config=config, task_dir=task_dir)
        with self._lock:
            self._tasks[task_id] = task
        threading.Thread(target=self._run_task, args=(task_id,), daemon=True).start()
        return task

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status not in {"queued", "running"}:
                return False
            task.cancel_requested = True
            task.status = "cancelling"
            if task.process:
                task.process.terminate()
            return True

    def _event(self, task_id: str, phase: str, message: str, **extra: Any) -> None:
        event = {"ts": time.time(), "phase": phase, "message": message}
        event.update(extra)
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            task.events.append(event)
            _append_jsonl(task.task_dir / "events.jsonl", event)

    def _log(self, task_id: str, line: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            task.log.append(line.rstrip("\n"))
            with (task.task_dir / "task.log").open("a", encoding="utf-8") as handle:
                handle.write(line.rstrip("\n") + "\n")

    def _set(
        self,
        task_id: str,
        *,
        status: str | None = None,
        process: subprocess.Popen[str] | None = None,
        returncode: int | None = None,
        error: str | None = None,
        artifact: tuple[str, Path] | None = None,
    ) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            if status is not None:
                task.status = status
            if process is not None:
                task.process = process
            if returncode is not None:
                task.returncode = returncode
            if error is not None:
                task.error = error
            if artifact is not None:
                name, path = artifact
                task.artifacts[name] = str(path)

    def _run_task(self, task_id: str) -> None:
        task = self.get(task_id)
        if not task:
            return
        try:
            steps = build_steps(task.config, task.task_dir)
        except ValueError as exc:
            self._set(task_id, status="failed", returncode=2, error=str(exc))
            self._event(task_id, "error", str(exc))
            return

        self._set(task_id, status="running")
        self._event(task_id, "init", f"任务已开始：{task.config.get('function')}")
        (task.task_dir / "task.json").write_text(
            json.dumps({"id": task.id, "config": task.config, "steps": [s.to_json() for s in steps]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        for index, step in enumerate(steps, start=1):
            if self.get(task_id) and self.get(task_id).cancel_requested:
                self._set(task_id, status="cancelled", returncode=-15)
                self._event(task_id, "cancelled", "任务已停止")
                return
            self._event(
                task_id,
                step.name,
                f"[{index}/{len(steps)}] {_step_action_label(step.artifact_name)}：{_artifact_label(step.artifact_name)}",
                artifact=step.artifact_name,
            )
            self._log(task_id, "$ " + " ".join(step.command))
            returncode = self._run_step(task_id, step)
            if returncode != 0:
                current = self.get(task_id)
                status = "cancelled" if current and current.cancel_requested else "failed"
                self._set(task_id, status=status, returncode=returncode)
                self._event(task_id, status, f"{_artifact_label(step.artifact_name)} 退出码：{returncode}")
                return
            self._set(task_id, artifact=(step.artifact_name, step.artifact_path))
            for artifact_name, artifact_path in _extra_artifacts_for_step(step):
                self._set(task_id, artifact=(artifact_name, artifact_path))
            for event in _harness_events_for_step(step):
                self._event(task_id, event["phase"], event["message"], artifact=event.get("artifact"))
            failure = _harness_failure_message_for_step(step)
            if failure:
                self._set(task_id, status="failed", returncode=1, error=failure)
                self._event(task_id, "failed", failure, artifact=step.artifact_name)
                return
            self._event(task_id, step.name, f"{_artifact_label(step.artifact_name)} 已完成", artifact=step.artifact_name)

        self._set(task_id, status="completed", returncode=0)
        self._event(task_id, "complete", _completion_message(task.task_dir))

    def _run_step(self, task_id: str, step: PipelineStep) -> int:
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
            self._log(task_id, f"启动失败：{exc}")
            return 127

        self._set(task_id, process=process)
        assert process.stdout is not None
        output_handle = step.artifact_path.open("w", encoding="utf-8") if step.capture_stdout else None
        try:
            for line in process.stdout:
                if output_handle:
                    output_handle.write(line)
                else:
                    self._log(task_id, line)
            return process.wait()
        finally:
            if output_handle:
                output_handle.close()


def build_steps(config: dict[str, Any], task_dir: Path) -> list[PipelineStep]:
    function = _required(config, "function")
    repo = _required(config, "repo")
    selected = _selected_artifacts(config)
    if not selected:
        raise ValueError("请至少选择一个 kRepo 产物或 AxF 后续流程")
    wants_harness_agent = _has_harness_agent(selected)

    common = ["--repo", repo]
    _add_optional(common, "--db", config.get("db"))
    _add_optional(common, "--file", config.get("file"))
    _add_optional(common, "--max-deps", config.get("max_deps"))
    _add_optional(common, "--max-candidates", config.get("max_candidates"))
    _add_optional(common, "--max-snippet-lines", config.get("max_snippet_lines"))

    steps: list[PipelineStep] = []
    if "report_md" in selected:
        steps.append(
            _capture_step("report_md", task_dir / "report.md", ["report", function, *common])
        )
    if "report_json" in selected or wants_harness_agent:
        steps.append(
            _capture_step("report_json", task_dir / "report.json", ["report", function, *common, "--format", "json"])
        )
    if "source" in selected:
        output = task_dir / f"{function}_source_bundle.c"
        command = _base_command("source", function, *common, "--output", str(output))
        steps.append(PipelineStep("source", command, "source", output, capture_stdout=False))
    if "subsource" in selected or wants_harness_agent:
        output = task_dir / f"{function}_subsource_bundle.c"
        command = _base_command("subsource", function, *common, "--output", str(output))
        _add_optional(command, "--max-depth", config.get("max_depth"))
        _add_optional(command, "--max-functions", config.get("max_functions"))
        steps.append(PipelineStep("subsource", command, "subsource", output, capture_stdout=False))
    if "calls" in selected or wants_harness_agent:
        command = ["calls", function, *common]
        _add_optional(command, "--max-depth", config.get("call_depth"))
        steps.append(_capture_step("calls", task_dir / "calls.txt", command))
    if "params" in selected or wants_harness_agent:
        steps.append(_capture_step("params", task_dir / "params.txt", ["params", function, *common]))
    if wants_harness_agent:
        harness_dir = task_dir / "harness"
        command = [
            sys.executable,
            "-m",
            "agents.harness_generation.agent",
            "--function",
            function,
            "--repo",
            repo,
            "--task-dir",
            str(task_dir),
            "--report-json",
            str(task_dir / "report.json"),
            "--subsource",
            str(task_dir / f"{function}_subsource_bundle.c"),
            "--calls",
            str(task_dir / "calls.txt"),
            "--params",
            str(task_dir / "params.txt"),
            "--out",
            str(harness_dir),
            "--artifact",
            str(task_dir / "generated_harness.txt"),
        ]
        _add_optional(command, "--file", config.get("file"))
        _add_optional(command, "--model", config.get("model"))
        _add_optional(command, "--chat-url", config.get("chat_url"))
        _add_optional(command, "--api-key-env", config.get("api_key_env"))
        _add_optional(command, "--timeout", config.get("model_timeout") or DEFAULT_MODEL_TIMEOUT)
        _add_optional(command, "--max-retries", config.get("model_max_retries") or DEFAULT_MODEL_MAX_RETRIES)
        _add_optional(command, "--clang", config.get("clang"))
        _add_optional(command, "--max-repair-rounds", config.get("max_repair_rounds"))
        _add_optional(command, "--compile-timeout", config.get("compile_timeout"))
        steps.append(
            PipelineStep(
                HARNESS_AGENT_ARTIFACT,
                command,
                HARNESS_AGENT_ARTIFACT,
                task_dir / "generated_harness.txt",
                capture_stdout=False,
            )
        )
    return steps


def default_config() -> dict[str, Any]:
    return {
        "repo": _default_repo_path(),
        "db": "",
        "function": "can_send",
        "file": "net/can/af_can.c",
        "artifacts": ["report_md", "report_json", "subsource", "calls", "params", HARNESS_AGENT_ARTIFACT],
        "model": "glm-5.1",
        "chat_url": "",
        "api_key_env": "API_KEY",
        "model_timeout": DEFAULT_MODEL_TIMEOUT,
        "model_max_retries": DEFAULT_MODEL_MAX_RETRIES,
        "clang": "",
        "max_repair_rounds": 3,
        "compile_timeout": 60,
        "max_deps": 50,
        "max_candidates": 12,
        "max_snippet_lines": 120,
        "max_depth": 1,
        "max_functions": 30,
        "call_depth": 3,
    }


def _default_repo_path() -> str:
    if (PROJECT_ROOT / "linux-7.0").exists():
        return "./linux-7.0"
    if (PROJECT_ROOT.parent / "linux-7.0").exists():
        return "../linux-7.0"
    return "./linux-7.0"


def serve(host: str = "127.0.0.1", port: int = 8787, open_browser: bool = False) -> None:
    store = TaskStore(DEFAULT_WORKSPACE)
    server = ThreadingHTTPServer((host, port), _handler_factory(store))
    url = f"http://{host}:{server.server_address[1]}"
    print(f"AxF 前端：{url}", flush=True)
    if open_browser:
        threading.Timer(0.2, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭", flush=True)
    finally:
        server.server_close()


def _handler_factory(store: TaskStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "AxFFrontend/0.1"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path == "/":
                self._send_static("index.html", "text/html; charset=utf-8")
            elif path.startswith("/static/"):
                self._send_static(path.removeprefix("/static/"), _content_type(path))
            elif path == "/api/defaults":
                self._send_json(default_config())
            elif path == "/api/tasks":
                self._send_json({"tasks": [task.to_json() for task in store.list()]})
            elif path.startswith("/api/tasks/"):
                self._handle_task_get(path, parsed.query)
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "未找到")

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path == "/api/tasks":
                try:
                    task = store.create(self._read_json())
                except ValueError as exc:
                    self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                self._send_json(task.to_json(), status=HTTPStatus.CREATED)
                return
            if path.startswith("/api/tasks/") and path.endswith("/cancel"):
                task_id = path.split("/")[3]
                if not store.cancel(task_id):
                    self._send_error(HTTPStatus.CONFLICT, "任务未在运行")
                    return
                task = store.get(task_id)
                self._send_json(task.to_json() if task else {})
                return
            self._send_error(HTTPStatus.NOT_FOUND, "未找到")

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _handle_task_get(self, path: str, query: str) -> None:
            parts = path.strip("/").split("/")
            if len(parts) < 3:
                self._send_error(HTTPStatus.NOT_FOUND, "未找到")
                return
            task = store.get(parts[2])
            if not task:
                self._send_error(HTTPStatus.NOT_FOUND, "任务不存在")
                return
            if len(parts) == 3:
                self._send_json(task.to_json())
                return
            if len(parts) == 4 and parts[3] == "artifact":
                name = parse_qs(query).get("name", [""])[0]
                path_text = task.artifacts.get(name)
                if not path_text:
                    self._send_error(HTTPStatus.NOT_FOUND, "产物不存在")
                    return
                self._send_text(_read_optional(Path(path_text)))
                return
            self._send_error(HTTPStatus.NOT_FOUND, "未找到")

        def _send_static(self, rel_path: str, content_type: str) -> None:
            target = (STATIC_DIR / rel_path).resolve()
            try:
                target.relative_to(STATIC_DIR.resolve())
            except ValueError:
                self._send_error(HTTPStatus.NOT_FOUND, "未找到")
                return
            if not target.is_file():
                self._send_error(HTTPStatus.NOT_FOUND, "未找到")
                return
            data = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_text(self, text: str) -> None:
            data = text.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_error(self, status: HTTPStatus, message: str) -> None:
            self._send_json({"error": message}, status=status)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSON 无效：{exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError("请求 JSON 必须是对象")
            return payload

    return Handler


def _base_command(command: str, function: str, *args: str) -> list[str]:
    return [
        sys.executable,
        str(PROJECT_ROOT / "knowledge_base" / "src" / "cpp_meta_query.py"),
        command,
        function,
        *[str(arg) for arg in args],
    ]


def _capture_step(name: str, output: Path, command: list[str]) -> PipelineStep:
    return PipelineStep(name, _base_command(*command), name, output, capture_stdout=True)


def _selected_artifacts(config: dict[str, Any]) -> set[str]:
    raw = config.get("artifacts")
    if isinstance(raw, list):
        return {str(item) for item in raw}
    if isinstance(raw, str):
        return {item.strip() for item in raw.split(",") if item.strip()}
    return set(default_config()["artifacts"])


def _has_harness_agent(selected: set[str]) -> bool:
    return HARNESS_AGENT_ARTIFACT in selected


def _required(config: dict[str, Any], key: str) -> str:
    value = str(config.get(key) or "").strip()
    if not value:
        raise ValueError(f"缺少必填字段：{key}")
    return value


def _artifact_label(name: str) -> str:
    return {
        "report_md": "Markdown 报告",
        "report_json": "JSON 报告",
        "source": "源码分析包",
        "subsource": "下游源码包",
        "calls": "上层调用链",
        "params": "入参约束",
        HARNESS_AGENT_ARTIFACT: "Harness 生成 Agent",
        "fuzz_harness": "Fuzz 驱动 harness.c",
        "harness_mocks_h": "Mock 头文件",
        "harness_mocks_c": "Mock 源文件",
        "harness_build_sh": "Unix 构建脚本",
        "harness_build_ps1": "Windows 构建脚本",
        "harness_spec": "Harness 规格",
        "harness_dict": "Fuzz 字典",
        "harness_compile_log": "编译日志",
        "harness_run_log": "10 秒运行日志",
        "harness_llm_transcript": "LLM 交互日志",
    }.get(name, name)


def _step_action_label(name: str) -> str:
    return "正在运行" if name == HARNESS_AGENT_ARTIFACT else "正在抽取"


def _extra_artifacts_for_step(step: PipelineStep) -> list[tuple[str, Path]]:
    if step.artifact_name != HARNESS_AGENT_ARTIFACT:
        return []
    harness_dir = step.artifact_path.parent / "harness"
    candidates = [
        ("fuzz_harness", harness_dir / "harness.c"),
        ("harness_mocks_h", harness_dir / "mocks.h"),
        ("harness_mocks_c", harness_dir / "mocks.c"),
        ("harness_build_sh", harness_dir / "build.sh"),
        ("harness_build_ps1", harness_dir / "build.ps1"),
        ("harness_spec", harness_dir / "harness_spec.json"),
        ("harness_dict", harness_dir / "dict.txt"),
        ("harness_compile_log", harness_dir / "compile.log"),
        ("harness_run_log", harness_dir / "run.log"),
        ("harness_llm_transcript", harness_dir / "llm_transcript.md"),
    ]
    return [(name, path) for name, path in candidates if path.exists()]


def _harness_events_for_step(step: PipelineStep) -> list[dict[str, str]]:
    if step.artifact_name != HARNESS_AGENT_ARTIFACT:
        return []
    spec = _read_harness_spec(step.artifact_path.parent)
    events: list[dict[str, str]] = []
    compile_info = spec.get("compile") if isinstance(spec.get("compile"), dict) else {}
    run_info = spec.get("run") if isinstance(spec.get("run"), dict) else {}
    if compile_info:
        events.append(
            {
                "phase": "harness_compile",
                "message": _compile_event_message(compile_info),
                "artifact": "harness_compile_log",
            }
        )
    if run_info:
        events.append(
            {
                "phase": "harness_run",
                "message": _run_event_message(run_info),
                "artifact": "harness_run_log",
            }
        )
    return events


def _harness_summary(task_dir: Path) -> dict[str, Any]:
    spec = _read_harness_spec(task_dir)
    return {
        "status": spec.get("status", ""),
        "classification": spec.get("classification", ""),
        "compile": spec.get("compile") if isinstance(spec.get("compile"), dict) else {},
        "run": spec.get("run") if isinstance(spec.get("run"), dict) else {},
    }


def _harness_failure_message_for_step(step: PipelineStep) -> str:
    if step.name != HARNESS_AGENT_ARTIFACT:
        return ""
    if not (step.artifact_path.parent / "harness" / "harness_spec.json").exists():
        return "任务未完成：Harness 生成 Agent 未写出 harness_spec.json"
    return _harness_failure_message(step.artifact_path.parent)


def _harness_failure_message(task_dir: Path) -> str:
    summary = _harness_summary(task_dir)
    if not summary.get("status"):
        return ""
    compile_info = summary.get("compile") if isinstance(summary.get("compile"), dict) else {}
    run_info = summary.get("run") if isinstance(summary.get("run"), dict) else {}
    harness_status = str(summary.get("status") or "")
    compile_status = str(compile_info.get("status") or "")
    run_status = str(run_info.get("status") or "")

    if run_status == "success":
        return ""
    if run_status == "failed":
        return "任务未完成：libFuzzer 试跑失败"
    if run_status == "timeout":
        return "任务未完成：libFuzzer 试跑超时"
    if compile_status == "failed":
        return "任务未完成：Harness 编译失败"
    if compile_status == "skipped":
        return "任务未完成：Harness 未编译"
    if compile_status == "success":
        return "任务未完成：Harness 已编译但未完成 libFuzzer 试跑"
    if harness_status == "unsupported":
        return "任务未完成：目标不支持自动生成"
    if harness_status == "needs_manual_fixture":
        return "任务未完成：需要手工 Fixture"
    if harness_status == "generated":
        return "任务未完成：Harness 仅生成，尚未编译验证"
    return ""


def _completion_message(task_dir: Path) -> str:
    failure = _harness_failure_message(task_dir)
    if failure:
        return failure
    summary = _harness_summary(task_dir)
    compile_info = summary.get("compile") if isinstance(summary.get("compile"), dict) else {}
    run_info = summary.get("run") if isinstance(summary.get("run"), dict) else {}
    harness_status = str(summary.get("status") or "")
    run_status = str(run_info.get("status") or "")
    compile_status = str(compile_info.get("status") or "")

    if run_status == "success":
        return "任务完成：libFuzzer 试跑通过"
    if run_status == "failed":
        return "任务完成：libFuzzer 试跑失败"
    if run_status == "timeout":
        return "任务完成：libFuzzer 试跑超时"
    if compile_status == "success":
        return "任务完成：Harness 编译通过"
    if compile_status == "failed":
        return "任务完成：Harness 编译失败"
    if compile_status == "skipped":
        return "任务完成：Harness 未编译"
    if harness_status == "unsupported":
        return "任务完成：目标不支持自动生成"
    if harness_status == "needs_manual_fixture":
        return "任务完成：需要手工 Fixture"
    if harness_status:
        return f"任务完成：Harness 状态 {harness_status}"
    return "任务完成：kRepo 知识抽取完成"


def _read_harness_spec(task_dir: Path) -> dict[str, Any]:
    spec_path = task_dir / "harness" / "harness_spec.json"
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return spec if isinstance(spec, dict) else {}


def _compile_event_message(compile_info: dict[str, Any]) -> str:
    status = str(compile_info.get("status") or "")
    attempts = compile_info.get("attempts")
    attempt_count = len(attempts) if isinstance(attempts, list) else 0
    attempt_text = f"（{attempt_count} 次尝试）" if attempt_count else ""
    message = str(compile_info.get("message") or "").strip()
    if status == "success":
        return f"Harness 编译通过{attempt_text}"
    if status == "failed":
        return f"Harness 编译失败{attempt_text}，日志见编译日志"
    if status == "skipped":
        return f"Harness 编译跳过：{message or '未执行编译'}"
    return f"Harness 编译状态：{status or '未知'}"


def _run_event_message(run_info: dict[str, Any]) -> str:
    status = str(run_info.get("status") or "")
    seconds = run_info.get("seconds") or 10
    message = str(run_info.get("message") or "").strip()
    if status == "success":
        return f"libFuzzer 试跑通过（{seconds} 秒），日志见 10 秒运行日志"
    if status == "timeout":
        return f"libFuzzer 试跑超时（{seconds} 秒），日志见 10 秒运行日志"
    if status == "failed":
        return f"libFuzzer 试跑失败（退出码 {run_info.get('returncode', '未知')}），日志见 10 秒运行日志"
    if status == "skipped":
        return f"libFuzzer 试跑跳过：{message or '未执行试跑'}"
    return f"libFuzzer 试跑状态：{status or '未知'}"


def _add_optional(command: list[str], flag: str, value: object) -> None:
    if value is None or value == "":
        return
    command.extend([flag, str(value)])


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _read_optional(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _content_type(path: str) -> str:
    if path.endswith(".css"):
        return "text/css; charset=utf-8"
    if path.endswith(".js"):
        return "text/javascript; charset=utf-8"
    if path.endswith(".html"):
        return "text/html; charset=utf-8"
    return "application/octet-stream"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="启动 AxF 本地前端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args(argv)
    try:
        serve(args.host, args.port, args.open)
    except OSError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
