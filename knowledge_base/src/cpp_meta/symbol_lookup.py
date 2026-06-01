from __future__ import annotations

from .base import CppMetaCommand, QueryOptions
from .db import find_symbols
from .dependencies import item_to_dict


SYMBOL_KIND_TO_DB_KIND = {
    "struct": 2,
    "union": 3,
    "enum": 4,
    "enumerator": 8,
    "typedef": 21,
    "variable": 28,
    "macro": 37,
    "macro_define": 37,
}


class SymbolLookupCommand(CppMetaCommand):
    """Feature 5: print source snippets for non-function symbols."""

    def build_report(self, symbol: str, *, kind: str | None = None) -> dict[str, object]:
        _repo_path, con = self.open_context()
        try:
            kinds = resolve_symbol_kinds(kind)
            candidates = find_symbols(con, symbol, self.options.file_filter, kinds)
            return {
                "symbol": symbol,
                "kind_filter": kind,
                "candidates": [
                    item_to_dict(item, self.options.max_snippet_lines)
                    for item in candidates[: self.options.max_candidates]
                ],
                "total_candidates": len(candidates),
                "limits": {
                    "max_candidates": self.options.max_candidates,
                    "max_snippet_lines": self.options.max_snippet_lines,
                },
                "notes": [
                    "符号检索默认只搜索非函数符号，不展开函数源码。",
                    "同名符号会按宏、typedef、枚举、变量、结构体/union 等顺序列出多个候选。",
                    "可使用 --kind 和 --file 收窄候选范围。",
                ],
            }
        finally:
            con.close()

    def print(self, symbol: str, *, kind: str | None = None) -> None:
        report = self.build_report(symbol, kind=kind)
        print_symbol_report(report)


def resolve_symbol_kinds(kind: str | None) -> tuple[int, ...]:
    if not kind:
        return tuple(sorted(set(SYMBOL_KIND_TO_DB_KIND.values())))
    normalized = kind.strip().lower()
    if normalized not in SYMBOL_KIND_TO_DB_KIND:
        choices = ", ".join(sorted(SYMBOL_KIND_TO_DB_KIND))
        raise SystemExit(f"unsupported symbol kind: {kind}. choices: {choices}")
    return (SYMBOL_KIND_TO_DB_KIND[normalized],)


def print_symbol_report(report: dict[str, object]) -> None:
    symbol = report["symbol"]
    candidates = report["candidates"]
    total = int(report["total_candidates"])
    print(f"Symbol: {symbol}")
    if report.get("kind_filter"):
        print(f"Kind filter: {report['kind_filter']}")
    print(f"Candidates: {len(candidates)} shown / {total} total")
    print()
    if not candidates:
        print("No non-function symbol found.")
        return

    for idx, item in enumerate(candidates, start=1):
        print(
            f"[{idx}] {item['kind']} {item['name']} "
            f"({item['file']}:{item['start_line']}-{item['end_line']})"
        )
        if item.get("type"):
            print(f"Type: {item['type']}")
        print("```c")
        snippet = str(item.get("snippet", "")).rstrip()
        if snippet:
            print(snippet)
        elif item.get("snippet_error"):
            print(f"/* source snippet unavailable: {item['snippet_error']} */")
        else:
            print("/* source snippet unavailable */")
        print("```")
        print()


def lookup_symbol_source(
    symbol: str,
    *,
    repo: str = ".",
    db: str | None = None,
    file_filter: str | None = None,
    kind: str | None = None,
    max_candidates: int = 12,
    max_snippet_lines: int = 80,
) -> dict[str, object]:
    """API 5: return non-function symbol source snippets."""
    command = SymbolLookupCommand(
        QueryOptions(
            repo=repo,
            db=db,
            file_filter=file_filter,
            max_candidates=max_candidates,
            max_snippet_lines=max_snippet_lines,
        )
    )
    return command.build_report(symbol, kind=kind)


def print_symbol_source(
    symbol: str,
    *,
    repo: str = ".",
    db: str | None = None,
    file_filter: str | None = None,
    kind: str | None = None,
    max_candidates: int = 12,
    max_snippet_lines: int = 80,
) -> None:
    """API 5: print non-function symbol source snippets."""
    command = SymbolLookupCommand(
        QueryOptions(
            repo=repo,
            db=db,
            file_filter=file_filter,
            max_candidates=max_candidates,
            max_snippet_lines=max_snippet_lines,
        )
    )
    command.print(symbol, kind=kind)
