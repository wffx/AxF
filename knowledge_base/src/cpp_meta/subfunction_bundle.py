from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from .base import CppMetaCommand, QueryOptions
from .engine import (
    DEPENDENCY_GROUP_NAMES,
    collect_downstream_functions,
    collect_function_dependencies,
    empty_dependency_groups,
    expand_nested_type_dependencies,
    function_params,
    item_to_dict,
    merge_dependency_groups,
    source_slice,
    discover_calls,
)
from .filters import exclude_test_symbol_items
from .renderer import default_bundle_path, render_subfunction_c_bundle


class SubfunctionBundleCommand(CppMetaCommand):
    """Feature 4: export target and downstream child functions into one .c bundle."""

    def build_report(
        self,
        function: str,
        *,
        include_auxiliary: bool = False,
        max_depth: int = 3,
        max_functions: int = 200,
        max_nesting_depth: int = 4,
    ) -> dict[str, object]:
        _repo_path, con = self.open_context()
        try:
            target, candidates = self.select_function(con, function)
            functions, edges, skipped_auxiliary = collect_downstream_functions(
                con,
                target,
                include_macros=self.options.include_macros,
                include_auxiliary=include_auxiliary,
                max_depth=max_depth,
                max_functions=max_functions,
            )
            dependencies = empty_dependency_groups()
            seen_dependency_ids: set[int] = set()
            function_reports: list[dict[str, object]] = []

            for function_item in functions:
                params = function_params(con, function_item.id)
                source = source_slice(function_item)
                function_deps = collect_function_dependencies(
                    con,
                    function_item,
                    source,
                    params,
                    expand_nested_types=False,
                    max_nesting_depth=max_nesting_depth,
                    exclude_test_symbols=True,
                )
                merge_dependency_groups(dependencies, function_deps, seen_dependency_ids)
                function_reports.append(
                    {
                        "item": asdict(function_item),
                        "source": source,
                        "calls": [
                            asdict(call)
                            for call in discover_calls(
                                con,
                                function_item,
                                source,
                                self.options.include_macros,
                            )
                        ],
                    }
                )

            for group_name in DEPENDENCY_GROUP_NAMES:
                dependencies[group_name] = dependencies[group_name][: self.options.max_deps]
            dependencies = expand_nested_type_dependencies(
                con,
                dependencies,
                target,
                max_nesting_depth,
                exclude_test_symbols=True,
            )
            dependencies = {
                group_name: exclude_test_symbol_items(items)
                for group_name, items in dependencies.items()
            }

            return {
                "selected": asdict(target),
                "ambiguous_candidates": [asdict(item) for item in candidates],
                "functions": function_reports,
                "edges": {str(parent): sorted(children) for parent, children in edges.items()},
                "skipped_auxiliary_calls": skipped_auxiliary,
                "dependencies": {
                    name: [
                        item_to_dict(item, self.options.max_snippet_lines)
                        for item in items[: self.options.max_deps]
                    ]
                    for name, items in dependencies.items()
                },
                "limits": {
                    "max_depth": max_depth,
                    "max_functions": max_functions,
                    "max_deps": self.options.max_deps,
                    "max_nesting_depth": max_nesting_depth,
                    "include_auxiliary": include_auxiliary,
                    "exclude_test_symbols": True,
                },
                "notes": [
                    "子函数集合来自源码直接调用启发式解析；函数指针和成员调用保留但不强行解析。",
                    "默认跳过日志、trace、debug、统计/accounting、instrumentation 等辅助调用。",
                    "默认排除 test/tests/testing/selftests/dt/st 等测试目录中的符号索引，避免测试符号混入导致重定义。",
                    "输出中的依赖片段和函数体会尽量按先定义后引用排序。",
                ],
            }
        finally:
            con.close()

    def export(
        self,
        function: str,
        output: str | Path | None = None,
        *,
        include_auxiliary: bool = False,
        max_depth: int = 3,
        max_functions: int = 200,
        max_nesting_depth: int = 4,
    ) -> Path:
        report = self.build_report(
            function,
            include_auxiliary=include_auxiliary,
            max_depth=max_depth,
            max_functions=max_functions,
            max_nesting_depth=max_nesting_depth,
        )
        output_path = Path(output) if output else default_bundle_path(report, suffix="subfunctions_bundle")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(render_subfunction_c_bundle(report), encoding="utf-8")
        return output_path


def export_subfunction_source_bundle(
    function: str,
    output: str | Path | None = None,
    *,
    repo: str = ".",
    db: str | None = None,
    file_filter: str | None = None,
    include_macros: bool = True,
    include_auxiliary: bool = False,
    max_depth: int = 3,
    max_functions: int = 200,
    max_deps: int = 500,
    max_candidates: int = 12,
    max_snippet_lines: int = 120,
    max_nesting_depth: int = 4,
) -> Path:
    """API 4: export target plus downstream child functions into one .c bundle."""
    command = SubfunctionBundleCommand(
        QueryOptions(
            repo=repo,
            db=db,
            file_filter=file_filter,
            include_macros=include_macros,
            max_deps=max_deps,
            max_candidates=max_candidates,
            max_snippet_lines=max_snippet_lines,
        )
    )
    return command.export(
        function,
        output,
        include_auxiliary=include_auxiliary,
        max_depth=max_depth,
        max_functions=max_functions,
        max_nesting_depth=max_nesting_depth,
    )

