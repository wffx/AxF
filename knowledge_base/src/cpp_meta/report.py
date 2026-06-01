from __future__ import annotations

import json
from dataclasses import asdict

from .base import CppMetaCommand, QueryOptions
from .engine import analyze_report, build_upstream_call_chains
from .renderer import print_markdown
from .subfunction_bundle import SubfunctionBundleCommand


REPORT_CALL_MAX_DEPTH = 5
REPORT_CALL_MAX_CHAINS = 200
REPORT_CALL_MAX_CALLERS_PER_LEVEL = 80
REPORT_SUBSOURCE_MAX_DEPTH = 3
REPORT_SUBSOURCE_MAX_FUNCTIONS = 200
REPORT_MAX_NESTING_DEPTH = 4
REPORT_INLINE_SOURCE_MAX_LINES = 80
REPORT_INLINE_DEP_SNIPPET_MAX_LINES = 20


class ReportCommand(CppMetaCommand):
    """Unified report command combining the four public analysis features."""

    def build(self, function: str) -> dict[str, object]:
        source_report = analyze_report(
            function,
            repo=self.options.repo,
            db=self.options.db,
            file_filter=self.options.file_filter,
            include_macros=self.options.include_macros,
            max_deps=self.options.max_deps,
            max_candidates=self.options.max_candidates,
            max_snippet_lines=self.options.max_snippet_lines,
            expand_nested_types=True,
            max_nesting_depth=REPORT_MAX_NESTING_DEPTH,
        )
        call_chains = self._build_call_chains(function)
        subfunction_report = SubfunctionBundleCommand(self.options).build_report(
            function,
            include_auxiliary=False,
            max_depth=REPORT_SUBSOURCE_MAX_DEPTH,
            max_functions=REPORT_SUBSOURCE_MAX_FUNCTIONS,
            max_nesting_depth=REPORT_MAX_NESTING_DEPTH,
        )
        source_report.update(
            {
                "report_kind": "unified",
                "limits": {
                    "max_nesting_depth": REPORT_MAX_NESTING_DEPTH,
                    "call_max_depth": REPORT_CALL_MAX_DEPTH,
                    "call_max_chains": REPORT_CALL_MAX_CHAINS,
                    "call_max_callers_per_level": REPORT_CALL_MAX_CALLERS_PER_LEVEL,
                    "subsource_max_depth": REPORT_SUBSOURCE_MAX_DEPTH,
                    "subsource_max_functions": REPORT_SUBSOURCE_MAX_FUNCTIONS,
                    "include_auxiliary": False,
                    "exclude_test_symbols": True,
                },
                "call_chains": call_chains,
                "subfunction_bundle": subfunction_report,
            }
        )
        compact_report_code_sections(source_report)
        return source_report

    def _build_call_chains(self, function: str) -> list[list[dict[str, object]]]:
        repo_path, con = self.open_context()
        try:
            target, _candidates = self.select_function(con, function)
            chains = build_upstream_call_chains(
                con,
                repo_path,
                target,
                max_depth=REPORT_CALL_MAX_DEPTH,
                max_chains=REPORT_CALL_MAX_CHAINS,
                max_callers_per_level=REPORT_CALL_MAX_CALLERS_PER_LEVEL,
            )
            return [
                [
                    {
                        "caller": asdict(site.caller),
                        "line": site.line,
                        "text": site.text,
                    }
                    for site in chain
                ]
                for chain in chains
            ]
        finally:
            con.close()

    def print(self, function: str, *, output_format: str = "markdown") -> None:
        report = self.build(function)
        if output_format == "json":
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print_markdown(report)


def compact_report_code_sections(report: dict[str, object]) -> None:
    """Keep report output compact by replacing long code blocks with locations."""
    selected = report.get("selected", {})
    if isinstance(selected, dict):
        compact_source_field(
            report,
            selected,
            max_lines=REPORT_INLINE_SOURCE_MAX_LINES,
        )

    dependencies = report.get("dependencies", {})
    if isinstance(dependencies, dict):
        for items in dependencies.values():
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        compact_snippet_field(
                            item,
                            max_lines=REPORT_INLINE_DEP_SNIPPET_MAX_LINES,
                        )

    sub_report = report.get("subfunction_bundle")
    if not isinstance(sub_report, dict):
        return
    sub_dependencies = sub_report.get("dependencies", {})
    if isinstance(sub_dependencies, dict):
        for items in sub_dependencies.values():
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        compact_snippet_field(
                            item,
                            max_lines=REPORT_INLINE_DEP_SNIPPET_MAX_LINES,
                        )

    functions = sub_report.get("functions", [])
    if isinstance(functions, list):
        for function_report in functions:
            if not isinstance(function_report, dict):
                continue
            item = function_report.get("item", {})
            if isinstance(item, dict):
                compact_source_field(function_report, item, max_lines=0)


def compact_source_field(
    container: dict[str, object],
    item: dict[str, object],
    *,
    max_lines: int,
) -> None:
    source = container.get("source")
    if not isinstance(source, str) or not source:
        return
    line_count = len(source.splitlines())
    if line_count <= max_lines:
        return
    container["source"] = ""
    container["source_omitted"] = True
    container["source_lines"] = line_count
    container["source_location"] = item_location(item)


def compact_snippet_field(item: dict[str, object], *, max_lines: int) -> None:
    snippet = item.get("snippet")
    if not isinstance(snippet, str) or not snippet:
        return
    line_count = int(item.get("end_line", 0)) - int(item.get("start_line", 0)) + 1
    if line_count <= max_lines:
        return
    item["snippet"] = ""
    item["snippet_omitted"] = True
    item["snippet_lines"] = line_count
    item["snippet_location"] = item_location(item)


def item_location(item: dict[str, object]) -> str:
    return f"{item.get('file', '<unknown>')}:{item.get('start_line', '?')}-{item.get('end_line', '?')}"

