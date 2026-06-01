from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
from dataclasses import asdict
from pathlib import Path

from .db import find_enclosing_function, resolve_function
from .filters import is_test_symbol_path
from .models import CONTROL_WORDS, CallSite, CallerSite, CodeItem
from .parsing import chunked, source_slice, strip_comments_and_strings


CALL_RE = re.compile(
    r"(?<![\w])(?P<expr>[A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*)*)\s*\("
)
AUXILIARY_CALL_PREFIXES = (
    "printk",
    "pr_",
    "dev_dbg",
    "dev_err",
    "dev_info",
    "dev_notice",
    "dev_warn",
    "dev_printk",
    "netdev_dbg",
    "netdev_err",
    "netdev_info",
    "netdev_warn",
    "trace_",
    "ftrace_",
    "debug_",
    "dbg_",
    "log_",
    "dump_",
    "seq_printf",
    "seq_put",
    "WARN",
    "warn_",
    "lockdep_",
    "kcov_",
    "kasan_",
    "kmsan_",
    "kmemleak_",
    "instrument_",
    "profile_",
    "perf_",
    "add_rchar",
    "add_wchar",
    "inc_syscr",
    "inc_syscw",
    "task_io_account_",
    "acct_",
    "account_",
)
AUXILIARY_CALL_CONTAINS = (
    "trace",
    "debug",
    "dbg",
    "log",
    "account",
    "acct",
)
AUXILIARY_PATH_PARTS = (
    "\\trace\\",
    "/trace/",
    "\\tracing\\",
    "/tracing/",
    "\\debug\\",
    "/debug/",
    "\\debugfs\\",
    "/debugfs/",
    "\\lockdep",
    "/lockdep",
    "\\kasan\\",
    "/kasan/",
    "\\kcov\\",
    "/kcov/",
    "\\kmemleak",
    "/kmemleak",
)


def discover_calls(
    con: sqlite3.Connection, function_item: CodeItem, source: str, include_macros: bool = True
) -> list[CallSite]:
    clean = strip_comments_and_strings(source)
    calls: list[CallSite] = []
    for offset, line in enumerate(clean.splitlines(), start=function_item.start_line):
        for match in CALL_RE.finditer(line):
            expr = re.sub(r"\s+", "", match.group("expr"))
            callee = re.split(r"->|\.", expr)[-1]
            if callee in CONTROL_WORDS:
                continue
            if callee == function_item.name and offset == function_item.start_line:
                continue
            if not include_macros and callee.isupper():
                continue
            resolved = None
            if "->" not in expr and "." not in expr:
                resolved = resolve_function(con, callee, function_item.file)
            calls.append(
                CallSite(
                    line=offset,
                    expression=expr,
                    callee=callee,
                    resolved_file=resolved.file if resolved else None,
                    resolved_line=resolved.start_line if resolved else None,
                )
            )
    return calls


def is_auxiliary_callee(name: str, callee: CodeItem | None = None) -> bool:
    """Heuristically identify logging, tracing, stats, and instrumentation calls."""
    lowered = name.lower()
    if any(lowered.startswith(prefix.lower()) for prefix in AUXILIARY_CALL_PREFIXES):
        return True
    if any(marker in lowered for marker in AUXILIARY_CALL_CONTAINS):
        return True
    if callee:
        path = callee.file.lower()
        if any(part in path for part in AUXILIARY_PATH_PARTS):
            return True
    return False


def line_has_call(line: str, callee_name: str) -> bool:
    clean = strip_comments_and_strings(line)
    return re.search(rf"\b{re.escape(callee_name)}\s*\(", clean) is not None


def function_line_has_call(function_item: CodeItem, line: int, callee_name: str) -> bool:
    try:
        clean_source = strip_comments_and_strings(source_slice(function_item))
    except OSError:
        return False
    offset = line - function_item.start_line
    clean_lines = clean_source.splitlines()
    if offset < 0 or offset >= len(clean_lines):
        return False
    return re.search(rf"\b{re.escape(callee_name)}\s*\(", clean_lines[offset]) is not None


def iter_call_matches_with_rg(repo: Path, callee_name: str) -> list[tuple[str, int, str]]:
    if shutil.which("rg") is None:
        return []
    pattern = rf"\b{re.escape(callee_name)}\s*\("
    cmd = [
        "rg",
        "--line-number",
        "--no-heading",
        "--color",
        "never",
        "--glob",
        "*.c",
        "--glob",
        "*.h",
        "--glob",
        "*.S",
        pattern,
        str(repo),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError:
        return []
    if proc.returncode not in (0, 1):
        return []
    matches: list[tuple[str, int, str]] = []
    for raw in proc.stdout.splitlines():
        match = re.match(r"^(.*?):(\d+):(.*)$", raw)
        if not match:
            continue
        path_text, line_text, source_line = match.groups()
        if line_has_call(source_line, callee_name):
            matches.append((path_text, int(line_text), source_line))
    return matches


def iter_call_matches_multi_with_rg(
    repo: Path, callee_names: set[str], chunk_size: int = 80
) -> list[tuple[str, str, int, str]]:
    rg_path = shutil.which("rg")
    if rg_path is None or not callee_names:
        return []
    matches: list[tuple[str, str, int, str]] = []
    names = sorted(callee_names, key=lambda value: (-len(value), value))
    for name_chunk in chunked(names, chunk_size):
        alternation = "|".join(re.escape(name) for name in name_chunk)
        pattern = rf"\b(?:{alternation})\s*\("
        cmd = [
            rg_path,
            "--line-number",
            "--no-heading",
            "--color",
            "never",
            "--glob",
            "*.c",
            "--glob",
            "*.h",
            "--glob",
            "*.S",
            pattern,
            str(repo),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            continue
        if proc.returncode not in (0, 1):
            continue
        line_call_re = re.compile(rf"\b({alternation})\s*\(")
        for raw in proc.stdout.splitlines():
            match = re.match(r"^(.*?):(\d+):(.*)$", raw)
            if not match:
                continue
            path_text, line_text, source_line = match.groups()
            clean_line = strip_comments_and_strings(source_line)
            for call_match in line_call_re.finditer(clean_line):
                matches.append((call_match.group(1), path_text, int(line_text), source_line))
    return matches


def iter_call_matches_fallback(repo: Path, callee_name: str) -> list[tuple[str, int, str]]:
    pattern = re.compile(rf"\b{re.escape(callee_name)}\s*\(")
    matches: list[tuple[str, int, str]] = []
    for path in repo.rglob("*"):
        if path.suffix.lower() not in {".c", ".h", ".s"}:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_no, line in enumerate(lines, start=1):
            if pattern.search(line) and line_has_call(line, callee_name):
                matches.append((str(path), line_no, line))
    return matches


def find_direct_callers(
    con: sqlite3.Connection,
    repo: Path,
    callee_name: str,
    target_ids: set[int] | None = None,
    max_callers: int = 100,
) -> list[CallerSite]:
    matches = iter_call_matches_with_rg(repo, callee_name)
    if not matches:
        matches = iter_call_matches_fallback(repo, callee_name)

    callers: dict[int, CallerSite] = {}
    for path_text, line, source_line in matches:
        caller = find_enclosing_function(con, path_text, line)
        if caller is None:
            continue
        if not function_line_has_call(caller, line, callee_name):
            continue
        if target_ids and caller.id in target_ids:
            continue
        if caller.name == callee_name:
            continue
        old = callers.get(caller.id)
        if old is None or line < old.line:
            callers[caller.id] = CallerSite(caller=caller, line=line, text=source_line.strip())

    return sorted(
        callers.values(),
        key=lambda site: (site.caller.file.lower(), site.line, site.caller.name),
    )[:max_callers]


def find_direct_callers_multi(
    con: sqlite3.Connection,
    repo: Path,
    callee_names: set[str],
    max_callers_per_name: int = 100,
) -> dict[str, list[CallerSite]]:
    grouped: dict[str, dict[int, CallerSite]] = {name: {} for name in callee_names}
    matches = iter_call_matches_multi_with_rg(repo, callee_names)
    if not matches:
        for name in callee_names:
            for path_text, line, source_line in iter_call_matches_fallback(repo, name):
                matches.append((name, path_text, line, source_line))

    function_call_cache: dict[tuple[int, int, str], bool] = {}
    for callee_name, path_text, line, _source_line in matches:
        caller = find_enclosing_function(con, path_text, line)
        if caller is None or caller.name == callee_name:
            continue
        cache_key = (caller.id, line, callee_name)
        valid = function_call_cache.get(cache_key)
        if valid is None:
            valid = function_line_has_call(caller, line, callee_name)
            function_call_cache[cache_key] = valid
        if not valid:
            continue
        old = grouped.setdefault(callee_name, {}).get(caller.id)
        if old is None or line < old.line:
            grouped[callee_name][caller.id] = CallerSite(
                caller=caller,
                line=line,
                text="",
            )

    result: dict[str, list[CallerSite]] = {}
    for name, callers in grouped.items():
        result[name] = sorted(
            callers.values(),
            key=lambda site: (site.caller.file.lower(), site.line, site.caller.name),
        )[:max_callers_per_name]
    return result


def build_upstream_call_chains(
    con: sqlite3.Connection,
    repo: Path,
    target: CodeItem,
    max_depth: int,
    max_chains: int,
    max_callers_per_level: int,
) -> list[list[CallerSite]]:
    direct_target = CallerSite(caller=target, line=target.start_line, text="")
    paths: list[list[CallerSite]] = [[direct_target]]
    terminal_paths: list[list[CallerSite]] = []
    caller_cache: dict[str, list[CallerSite]] = {}

    for _depth in range(max_depth):
        expandable = [path for path in paths if len(path) <= max_depth]
        if not expandable:
            break
        needed_names = {path[0].caller.name for path in expandable if path[0].caller.name not in caller_cache}
        if needed_names:
            caller_cache.update(
                find_direct_callers_multi(
                    con,
                    repo,
                    needed_names,
                    max_callers_per_name=max_callers_per_level,
                )
            )

        next_paths: list[list[CallerSite]] = []
        extended_any = False
        for path in paths:
            top_name = path[0].caller.name
            seen_ids = {site.caller.id for site in path}
            callers = [
                site
                for site in caller_cache.get(top_name, [])
                if site.caller.id not in seen_ids
            ]
            if not callers:
                terminal_paths.append(path)
                continue
            extended_any = True
            for site in callers:
                next_paths.append([site, *path])
                if len(next_paths) >= max_chains:
                    break
            if len(next_paths) >= max_chains:
                break
        paths = next_paths[:max_chains]
        if not extended_any:
            break

    all_paths = terminal_paths + paths
    unique: list[list[CallerSite]] = []
    seen_paths: set[tuple[int, ...]] = set()
    for chain in all_paths:
        key = tuple(site.caller.id for site in chain)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        unique.append(chain)
    return unique[:max_chains]


def collect_downstream_functions(
    con: sqlite3.Connection,
    target: CodeItem,
    *,
    include_macros: bool = True,
    include_auxiliary: bool = False,
    max_depth: int = 3,
    max_functions: int = 200,
) -> tuple[list[CodeItem], dict[int, set[int]], list[dict[str, object]]]:
    """Collect the target and directly resolvable downstream callees."""
    functions_by_id: dict[int, CodeItem] = {target.id: target}
    edges: dict[int, set[int]] = {target.id: set()}
    skipped_auxiliary: list[dict[str, object]] = []
    skipped_keys: set[tuple[int, str, int]] = set()
    queue: list[tuple[CodeItem, int]] = [(target, 0)]
    queued: set[int] = {target.id}

    while queue:
        function_item, depth = queue.pop(0)
        queued.discard(function_item.id)
        try:
            source = source_slice(function_item)
        except OSError:
            continue
        if depth >= max_depth:
            continue
        calls = discover_calls(con, function_item, source, include_macros=include_macros)
        for call in calls:
            if "->" in call.expression or "." in call.expression:
                continue
            if not include_macros and call.callee.isupper():
                continue
            callee = resolve_function(con, call.callee, function_item.file)
            if callee is None or callee.id == function_item.id:
                continue
            if is_test_symbol_path(callee.file):
                continue
            if callee.span <= 0:
                continue
            if not include_auxiliary and is_auxiliary_callee(call.callee, callee):
                skipped_key = (function_item.id, call.callee, call.line)
                if skipped_key not in skipped_keys:
                    skipped_keys.add(skipped_key)
                    skipped_auxiliary.append(
                        {
                            "caller": asdict(function_item),
                            "callee": asdict(callee),
                            "name": call.callee,
                            "line": call.line,
                            "expression": call.expression,
                        }
                    )
                continue
            edges.setdefault(function_item.id, set()).add(callee.id)
            edges.setdefault(callee.id, set())
            if callee.id not in functions_by_id and len(functions_by_id) >= max_functions:
                continue
            if callee.id not in functions_by_id:
                functions_by_id[callee.id] = callee
            if depth < max_depth and callee.id not in queued:
                queue.append((callee, depth + 1))
                queued.add(callee.id)

    return order_functions_for_bundle(target.id, functions_by_id, edges), edges, skipped_auxiliary


def order_functions_for_bundle(
    root_id: int,
    functions_by_id: dict[int, CodeItem],
    edges: dict[int, set[int]],
) -> list[CodeItem]:
    """Return callees before callers so function bodies do not reference later bodies."""
    ordered: list[CodeItem] = []
    permanent: set[int] = set()
    temporary: set[int] = set()

    def visit(function_id: int) -> None:
        if function_id in permanent:
            return
        if function_id in temporary:
            return
        temporary.add(function_id)
        children = sorted(
            (child for child in edges.get(function_id, set()) if child in functions_by_id),
            key=lambda child: (
                functions_by_id[child].file.lower(),
                functions_by_id[child].start_line,
                functions_by_id[child].name,
            ),
        )
        for child_id in children:
            visit(child_id)
        temporary.remove(function_id)
        permanent.add(function_id)
        ordered.append(functions_by_id[function_id])

    visit(root_id)
    for function_id in sorted(
        functions_by_id,
        key=lambda item_id: (
            functions_by_id[item_id].file.lower(),
            functions_by_id[item_id].start_line,
            functions_by_id[item_id].name,
        ),
    ):
        visit(function_id)
    return ordered

