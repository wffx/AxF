from __future__ import annotations

import re
import json
import subprocess
from pathlib import Path
from typing import Any

from .krepo_config import KRepoConfig, resolve_krepo_config
from .krepo_models import KRepoArtifact, KRepoCommandResult, KRepoStepResult


class KRepoAdapter:
    def __init__(self, config: KRepoConfig | None = None):
        self.config = config or resolve_krepo_config()

    @classmethod
    def from_inputs(cls, inputs: dict[str, Any]) -> "KRepoAdapter":
        config = resolve_krepo_config(
            inputs.get("krepo_root") or inputs.get("krepo"),
            provider=inputs.get("krepo_provider"),
        )
        return cls(config)

    def report(
        self,
        *,
        function: str,
        repo: str | Path,
        artifact_dir: str | Path,
        file: str | None = None,
        db: str | None = None,
        max_deps: int | None = None,
        max_candidates: int | None = None,
        max_snippet_lines: int | None = None,
        formats: tuple[str, ...] = ("json", "markdown"),
    ) -> KRepoStepResult:
        call_args = {
            "function": function,
            "repo": repo,
            "artifact_dir": artifact_dir,
            "file": file,
            "db": db,
            "max_deps": max_deps,
            "max_candidates": max_candidates,
            "max_snippet_lines": max_snippet_lines,
            "formats": formats,
        }
        out_dir = self._krepo_dir(artifact_dir)
        commands: list[KRepoCommandResult] = []
        artifacts: list[KRepoArtifact] = []
        json_output: Path | None = None
        for output_format in formats:
            if output_format not in {"json", "markdown"}:
                raise ValueError(f"unsupported report format: {output_format}")
            suffix = "json" if output_format == "json" else "md"
            output = out_dir / f"report.{suffix}"
            if output_format == "markdown" and json_output and json_output.exists():
                _write_markdown_report_from_json(json_output, output, function)
                artifacts.append(KRepoArtifact("report_md", output, "report_md"))
                continue
            command = [
                "report",
                function,
                *self._common_args(repo, file=file, db=db, max_deps=max_deps, max_candidates=max_candidates, max_snippet_lines=max_snippet_lines),
            ]
            if output_format == "json":
                command.extend(["--format", "json"])
            result = self._run_capture(command, output)
            commands.append(result)
            artifacts.append(KRepoArtifact(f"report_{suffix}", output, f"report_{suffix}"))
            if output_format == "json" and result.ok:
                json_output = output
        result = KRepoStepResult(self.config.provider, tuple(artifacts), tuple(commands))
        return self._maybe_fallback(result, "report", call_args)

    def params(
        self,
        *,
        function: str,
        repo: str | Path,
        artifact_dir: str | Path,
        file: str | None = None,
        db: str | None = None,
        max_deps: int | None = None,
        max_candidates: int | None = None,
        max_snippet_lines: int | None = None,
    ) -> KRepoStepResult:
        call_args = {
            "function": function,
            "repo": repo,
            "artifact_dir": artifact_dir,
            "file": file,
            "db": db,
            "max_deps": max_deps,
            "max_candidates": max_candidates,
            "max_snippet_lines": max_snippet_lines,
        }
        output = self._krepo_dir(artifact_dir) / "params.txt"
        result = self._run_capture(
            [
                "params",
                function,
                *self._common_args(repo, file=file, db=db, max_deps=max_deps, max_candidates=max_candidates, max_snippet_lines=max_snippet_lines),
            ],
            output,
        )
        artifact = KRepoArtifact("params", output, "params_text")
        step_result = KRepoStepResult(self.config.provider, (artifact,), (result,))
        return self._maybe_fallback(step_result, "params", call_args)

    def calls(
        self,
        *,
        function: str,
        repo: str | Path,
        artifact_dir: str | Path,
        file: str | None = None,
        db: str | None = None,
        max_deps: int | None = None,
        max_candidates: int | None = None,
        max_snippet_lines: int | None = None,
        call_depth: int | None = None,
    ) -> KRepoStepResult:
        call_args = {
            "function": function,
            "repo": repo,
            "artifact_dir": artifact_dir,
            "file": file,
            "db": db,
            "max_deps": max_deps,
            "max_candidates": max_candidates,
            "max_snippet_lines": max_snippet_lines,
            "call_depth": call_depth,
        }
        output = self._krepo_dir(artifact_dir) / "calls.txt"
        command = [
            "calls",
            function,
            *self._common_args(repo, file=file, db=db, max_deps=max_deps, max_candidates=max_candidates, max_snippet_lines=max_snippet_lines),
        ]
        if call_depth is not None:
            command.extend(["--max-depth", str(call_depth)])
        result = self._run_capture(command, output)
        artifact = KRepoArtifact("calls", output, "calls_text")
        step_result = KRepoStepResult(self.config.provider, (artifact,), (result,))
        return self._maybe_fallback(step_result, "calls", call_args)

    def subsource(
        self,
        *,
        function: str,
        repo: str | Path,
        artifact_dir: str | Path,
        file: str | None = None,
        db: str | None = None,
        max_deps: int | None = None,
        max_candidates: int | None = None,
        max_snippet_lines: int | None = None,
        max_depth: int | None = None,
        max_functions: int | None = None,
    ) -> KRepoStepResult:
        call_args = {
            "function": function,
            "repo": repo,
            "artifact_dir": artifact_dir,
            "file": file,
            "db": db,
            "max_deps": max_deps,
            "max_candidates": max_candidates,
            "max_snippet_lines": max_snippet_lines,
            "max_depth": max_depth,
            "max_functions": max_functions,
        }
        output = self._krepo_dir(artifact_dir) / "subsource.c"
        command = [
            "subsource",
            function,
            *self._common_args(repo, file=file, db=db, max_deps=max_deps, max_candidates=max_candidates, max_snippet_lines=max_snippet_lines),
            "--output",
            str(output),
        ]
        if max_depth is not None:
            command.extend(["--max-depth", str(max_depth)])
        if max_functions is not None:
            command.extend(["--max-functions", str(max_functions)])
        result = self._run_passthrough(command)
        artifact = KRepoArtifact("subsource", output, "subsource_c")
        step_result = KRepoStepResult(self.config.provider, (artifact,), (result,))
        return self._maybe_fallback(step_result, "subsource", call_args)

    def symbol(
        self,
        *,
        symbol: str,
        repo: str | Path,
        artifact_dir: str | Path,
        kind: str | None = None,
        file: str | None = None,
        db: str | None = None,
        max_candidates: int | None = None,
        max_snippet_lines: int | None = None,
    ) -> KRepoStepResult:
        call_args = {
            "symbol": symbol,
            "repo": repo,
            "artifact_dir": artifact_dir,
            "kind": kind,
            "file": file,
            "db": db,
            "max_candidates": max_candidates,
            "max_snippet_lines": max_snippet_lines,
        }
        out_dir = self._krepo_dir(artifact_dir) / "symbols"
        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / f"{_safe_name(symbol)}.txt"
        command = [
            "symbol",
            symbol,
            *self._common_args(repo, file=file, db=db, max_candidates=max_candidates, max_snippet_lines=max_snippet_lines),
        ]
        if kind:
            command.extend(["--kind", kind])
        result = self._run_capture(command, output)
        artifact = KRepoArtifact("symbol", output, "symbol_text")
        step_result = KRepoStepResult(self.config.provider, (artifact,), (result,))
        return self._maybe_fallback(step_result, "symbol", call_args)

    def _common_args(
        self,
        repo: str | Path,
        *,
        file: str | None = None,
        db: str | None = None,
        max_deps: int | None = None,
        max_candidates: int | None = None,
        max_snippet_lines: int | None = None,
    ) -> list[str]:
        args = ["--repo", str(repo)]
        if db:
            args.extend(["--db", str(db)])
        if file:
            args.extend(["--file", str(file)])
        if max_deps is not None:
            args.extend(["--max-deps", str(max_deps)])
        if max_candidates is not None:
            args.extend(["--max-candidates", str(max_candidates)])
        if max_snippet_lines is not None:
            args.extend(["--max-snippet-lines", str(max_snippet_lines)])
        return args

    def _krepo_dir(self, artifact_dir: str | Path) -> Path:
        out_dir = Path(artifact_dir) / "krepo"
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir

    def _full_command(self, args: list[str]) -> list[str]:
        return [self.config.python, str(self.config.query_script), *args]

    def _run_capture(self, args: list[str], output: Path) -> KRepoCommandResult:
        command = self._full_command(args)
        output.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            command,
            cwd=self.config.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode == 0:
            output.write_text(completed.stdout, encoding="utf-8")
        return KRepoCommandResult(tuple(command), completed.returncode, completed.stdout, completed.stderr)

    def _run_passthrough(self, args: list[str]) -> KRepoCommandResult:
        command = self._full_command(args)
        completed = subprocess.run(
            command,
            cwd=self.config.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return KRepoCommandResult(tuple(command), completed.returncode, completed.stdout, completed.stderr)

    def _maybe_fallback(
        self,
        result: KRepoStepResult,
        method_name: str,
        call_args: dict[str, Any],
    ) -> KRepoStepResult:
        if result.ok:
            return result
        if self.config.provider != "external" or not self.config.fallback_to_builtin:
            return result
        fallback = KRepoAdapter(resolve_krepo_config(provider="builtin"))
        method = getattr(fallback, method_name)
        return method(**call_args)


def run_report_skill(inputs: dict[str, Any]) -> dict[str, Any]:
    result = KRepoAdapter.from_inputs(inputs).report(**_function_inputs(inputs))
    return _skill_outputs(result)


def run_params_skill(inputs: dict[str, Any]) -> dict[str, Any]:
    result = KRepoAdapter.from_inputs(inputs).params(**_function_inputs(inputs))
    return _skill_outputs(result)


def run_calls_skill(inputs: dict[str, Any]) -> dict[str, Any]:
    call_inputs = _function_inputs(inputs)
    call_inputs["call_depth"] = _int_or_none(inputs.get("call_depth"))
    result = KRepoAdapter.from_inputs(inputs).calls(**call_inputs)
    return _skill_outputs(result)


def run_subsource_skill(inputs: dict[str, Any]) -> dict[str, Any]:
    subsource_inputs = _function_inputs(inputs)
    subsource_inputs["max_depth"] = _int_or_none(inputs.get("max_depth"))
    subsource_inputs["max_functions"] = _int_or_none(inputs.get("max_functions"))
    result = KRepoAdapter.from_inputs(inputs).subsource(**subsource_inputs)
    return _skill_outputs(result)


def run_symbol_skill(inputs: dict[str, Any]) -> dict[str, Any]:
    symbol = str(inputs.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("symbol is required")
    result = KRepoAdapter.from_inputs(inputs).symbol(
        symbol=symbol,
        repo=_required_input(inputs, "repo"),
        artifact_dir=_required_input(inputs, "artifact_dir"),
        kind=inputs.get("kind"),
        file=inputs.get("file"),
        db=inputs.get("db"),
        max_candidates=_int_or_none(inputs.get("max_candidates")),
        max_snippet_lines=_int_or_none(inputs.get("max_snippet_lines")),
    )
    return _skill_outputs(result)


def _function_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    return {
        "function": _required_input(inputs, "function"),
        "repo": _required_input(inputs, "repo"),
        "artifact_dir": _required_input(inputs, "artifact_dir"),
        "file": inputs.get("file"),
        "db": inputs.get("db"),
        "max_deps": _int_or_none(inputs.get("max_deps")),
        "max_candidates": _int_or_none(inputs.get("max_candidates")),
        "max_snippet_lines": _int_or_none(inputs.get("max_snippet_lines")),
    }


def _skill_outputs(result: KRepoStepResult) -> dict[str, Any]:
    failed = [command for command in result.commands if not command.ok]
    if failed:
        raise RuntimeError(failed[0].diagnostic())
    return {
        "provider": result.provider,
        "outputs": result.outputs(),
        "commands": [list(command.command) for command in result.commands],
    }


def _required_input(inputs: dict[str, Any], key: str) -> str:
    value = str(inputs.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return safe or "symbol"


def _write_markdown_report_from_json(json_path: Path, output: Path, function: str) -> None:
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    lines = [f"# kRepo Report: {function}", ""]
    if isinstance(data, dict):
        target = data.get("target") or data.get("function") or {}
        if target:
            lines.extend(["## Target", ""])
            if isinstance(target, dict):
                for key in ("name", "file", "line", "signature"):
                    if target.get(key) is not None:
                        lines.append(f"- `{key}`: `{target[key]}`")
            else:
                lines.append(f"- `{target}`")
            lines.append("")
        for key in ("direct_calls", "call_chains", "param_constraints", "subfunctions"):
            value = data.get(key)
            if isinstance(value, list):
                lines.append(f"- `{key}`: {len(value)} item(s)")
        if len(lines) <= 2:
            lines.append("JSON report was generated; see `report.json` for full details.")
    else:
        lines.append("JSON report was generated; see `report.json` for full details.")
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
