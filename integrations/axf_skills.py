from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_preflight_skill(inputs: dict[str, Any]) -> dict[str, Any]:
    repo = Path(_required(inputs, "repo"))
    function = _required(inputs, "function")
    file_filter = str(inputs.get("file") or "")
    db = str(inputs.get("db") or "")
    db_path = Path(db) if db else repo / ".vscode" / "BROWSE.VC.DB"
    outputs = {
        "repo": str(repo),
        "function": function,
        "file": file_filter,
        "db": str(db_path),
        "repo_exists": repo.exists(),
        "db_exists": db_path.exists(),
    }
    if not repo.exists():
        raise RuntimeError(f"repo not found: {repo}")
    return {"outputs": outputs}


def run_harness_generation_skill(inputs: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(_required(inputs, "run_dir"))
    artifact_dir = Path(_required(inputs, "artifact_dir"))
    repo = _required(inputs, "repo")
    function = _required(inputs, "function")
    harness_dir = artifact_dir / "harness"
    output_preview = run_dir / "generated_harness.txt"
    krepo_dir = artifact_dir / "krepo"
    command = [
        sys.executable,
        "-m",
        "agents.harness_generation.agent",
        "--function",
        function,
        "--repo",
        repo,
        "--task-dir",
        str(run_dir),
        "--out",
        str(harness_dir),
        "--artifact",
        str(output_preview),
    ]
    _add_optional(command, "--file", inputs.get("file"))
    _add_optional(command, "--llm-mode", inputs.get("llm_mode"))
    _add_optional(command, "--model", inputs.get("model"))
    _add_optional(command, "--chat-url", inputs.get("chat_url"))
    _add_optional(command, "--api-key-env", inputs.get("api_key_env") or "API_KEY")
    _add_optional(command, "--opencode-tool", inputs.get("opencode_tool"))
    _add_optional(command, "--opencode-executable", inputs.get("opencode_executable"))
    _add_optional(command, "--opencode-model", inputs.get("opencode_model"))
    _add_optional(command, "--timeout", inputs.get("model_timeout"))
    _add_optional(command, "--max-retries", inputs.get("model_max_retries"))
    _add_optional(command, "--clang", inputs.get("clang"))
    _add_optional(command, "--clang-mode", inputs.get("clang_mode") or "native")
    _add_optional(command, "--max-repair-rounds", inputs.get("max_repair_rounds"))
    _add_optional(command, "--compile-timeout", inputs.get("compile_timeout"))
    _add_existing(command, "--report-json", krepo_dir / "report.json")
    _add_existing(command, "--subsource", krepo_dir / "subsource.c")
    _add_existing(command, "--calls", krepo_dir / "calls.txt")
    _add_existing(command, "--params", krepo_dir / "params.txt")
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        env=os.environ.copy(),
    )
    log_path = run_dir / "logs" / "harness_generation.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError((completed.stdout or f"harness agent exited {completed.returncode}")[-4000:])
    outputs = {
        "harness_dir": str(harness_dir),
        "generated_harness": str(output_preview),
        "harness_spec": str(harness_dir / "harness_spec.json"),
        "command": command,
    }
    return {"outputs": outputs, "commands": [command]}


def run_report_skill(inputs: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(_required(inputs, "run_dir"))
    steps = inputs.get("steps") or {}
    report_path = run_dir / "report.md"
    lines = [
        "# AxF Workflow Report",
        "",
        f"- Function: `{inputs.get('function') or ''}`",
        f"- Repo: `{inputs.get('repo') or ''}`",
        "",
        "## Step Outputs",
        "",
    ]
    for step_id, outputs in steps.items():
        lines.append(f"### {step_id}")
        if isinstance(outputs, dict) and outputs:
            for key, value in sorted(outputs.items()):
                lines.append(f"- `{key}`: `{value}`")
        else:
            lines.append("- no outputs")
        lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return {"outputs": {"report_md": str(report_path)}}


def _required(inputs: dict[str, Any], key: str) -> str:
    value = str(inputs.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _add_optional(command: list[str], flag: str, value: Any) -> None:
    if value is None or value == "":
        return
    command.extend([flag, str(value)])


def _add_existing(command: list[str], flag: str, path: Path) -> None:
    if path.exists():
        command.extend([flag, str(path)])
