from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict

from .calls import CALL_RE
from .db import item_query, row_to_item
from .filters import exclude_test_symbol_items
from .models import C_KEYWORDS, DEPENDENCY_GROUP_NAMES, INTERESTING_KINDS, CodeItem
from .parsing import (
    discover_local_names,
    source_slice,
    strip_comments_and_strings,
    tokens_from_source,
    chunked,
)


def empty_dependency_groups() -> dict[str, list[CodeItem]]:
    return {name: [] for name in DEPENDENCY_GROUP_NAMES}


def merge_dependency_groups(
    target: dict[str, list[CodeItem]],
    source: dict[str, list[CodeItem]],
    seen_ids: set[int] | None = None,
) -> None:
    if seen_ids is None:
        seen_ids = {item.id for items in target.values() for item in items}
    for group_name in DEPENDENCY_GROUP_NAMES:
        for item in source[group_name]:
            if item.id in seen_ids:
                continue
            target[group_name].append(item)
            seen_ids.add(item.id)
def score_item(item: CodeItem, function_item: CodeItem, source_text: str) -> tuple[int, int]:
    path = item.file.lower()
    func_path = function_item.file.lower()
    score = 0
    if path == func_path:
        if item.kind in ("struct", "union", "enum", "typedef"):
            score += 100
        else:
            score += 1000
    if "\\include\\" in path or "/include/" in path:
        score += 80
    if item.parent_id == 1024:
        score += 40
    if item.attributes & 2:
        score += 500
    else:
        score -= 30
    if item.span > 0:
        score += min(item.span, 200)
    if item.kind == "macro_define" and re.search(rf"\b{re.escape(item.name)}\s*\(", source_text):
        score -= 40
    return (score, item.span)


def lookup_items_for_tokens(
    con: sqlite3.Connection,
    tokens: set[str],
    function_item: CodeItem,
    source_text: str,
    kinds: tuple[int, ...] = INTERESTING_KINDS,
    exclude_test_symbols: bool = False,
) -> list[CodeItem]:
    if not tokens:
        return []
    found: list[CodeItem] = []
    token_list = sorted(tokens)
    kind_placeholders = ",".join("?" for _ in kinds)
    for names in chunked(token_list):
        placeholders = ",".join("?" for _ in names)
        rows = con.execute(
            item_query(f"ci.kind in ({kind_placeholders}) and ci.name in ({placeholders})"),
            (*kinds, *names),
        ).fetchall()
        found.extend(row_to_item(row) for row in rows)

    if exclude_test_symbols:
        found = exclude_test_symbol_items(found)

    best_by_name_kind: dict[tuple[str, str], CodeItem] = {}
    for item in found:
        key = (item.name, item.kind)
        old = best_by_name_kind.get(key)
        if old is None or score_item(item, function_item, source_text) > score_item(
            old, function_item, source_text
        ):
            best_by_name_kind[key] = item
    return sorted(
        best_by_name_kind.values(),
        key=lambda item: (-score_item(item, function_item, source_text)[0], item.kind, item.name),
    )


def classify_dependencies(
    items: list[CodeItem], ignored_names: set[str], type_names: set[str]
) -> dict[str, list[CodeItem]]:
    groups = {
        "structures": [],
        "typedefs": [],
        "enums": [],
        "constants": [],
        "global_variables": [],
        "static_variables": [],
    }
    seen_static: set[int] = set()
    macro_names = {item.name for item in items if item.kind == "macro_define"}
    for item in items:
        if item.name in ignored_names and item.name not in type_names:
            continue
        if item.kind in ("struct", "union"):
            groups["structures"].append(item)
        elif item.kind == "typedef":
            groups["typedefs"].append(item)
        elif item.kind in ("enum", "enumerator"):
            groups["enums"].append(item)
        elif item.kind == "macro_define":
            groups["constants"].append(item)
        elif item.kind == "variable":
            if item.name in ignored_names or item.name in macro_names:
                continue
            line = ""
            try:
                line = source_slice(item, max_lines=1)
            except OSError:
                pass
            if re.search(r"\bstatic\b", line):
                groups["static_variables"].append(item)
                seen_static.add(item.id)
            else:
                groups["global_variables"].append(item)
    groups["global_variables"] = [
        item for item in groups["global_variables"] if item.id not in seen_static
    ]
    return groups


def extract_type_dependency_tokens(source: str) -> set[str]:
    clean = strip_comments_and_strings(source)
    tokens = tokens_from_source(clean)
    tokens.update(re.findall(r"\b(?:struct|union|enum)\s+([A-Za-z_]\w*)\b", clean))
    tokens.difference_update(C_KEYWORDS)
    return tokens


def extract_tag_type_tokens(source: str) -> set[str]:
    clean = strip_comments_and_strings(source)
    return set(re.findall(r"\b(?:struct|union|enum)\s+([A-Za-z_]\w*)\b", clean))


def lookup_nested_type_items(
    con: sqlite3.Connection,
    source: str,
    function_item: CodeItem,
    exclude_test_symbols: bool = False,
) -> list[CodeItem]:
    tag_tokens = extract_tag_type_tokens(source)
    typedef_tokens = tokens_from_source(source)
    typedef_tokens.difference_update(tag_tokens)

    found: list[CodeItem] = []
    if tag_tokens:
        found.extend(
            lookup_items_for_tokens(
                con,
                tag_tokens,
                function_item,
                source,
                kinds=(2, 3, 4),
                exclude_test_symbols=exclude_test_symbols,
            )
        )
    if typedef_tokens:
        found.extend(
            lookup_items_for_tokens(
                con,
                typedef_tokens,
                function_item,
                source,
                kinds=(21,),
                exclude_test_symbols=exclude_test_symbols,
            )
        )

    seen: set[int] = set()
    unique: list[CodeItem] = []
    for item in found:
        if item.id in seen:
            continue
        seen.add(item.id)
        unique.append(item)
    return unique


def append_unique_dependency(
    groups: dict[str, list[CodeItem]], item: CodeItem, seen_ids: set[int]
) -> bool:
    if item.id in seen_ids:
        return False
    if item.kind in ("struct", "union"):
        groups["structures"].append(item)
    elif item.kind == "typedef":
        groups["typedefs"].append(item)
    elif item.kind == "enum":
        groups["enums"].append(item)
    else:
        return False
    seen_ids.add(item.id)
    return True


def expand_nested_type_dependencies(
    con: sqlite3.Connection,
    dependencies: dict[str, list[CodeItem]],
    function_item: CodeItem,
    max_depth: int,
    exclude_test_symbols: bool = False,
) -> dict[str, list[CodeItem]]:
    if max_depth <= 0:
        return dependencies

    expanded = {name: list(items) for name, items in dependencies.items()}
    seen_ids = {item.id for items in expanded.values() for item in items}
    queue: list[tuple[CodeItem, int]] = [
        (item, 0)
        for group_name in ("structures", "typedefs", "enums")
        for item in expanded[group_name]
    ]

    while queue:
        item, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        try:
            item_source = source_slice(item)
        except OSError:
            continue
        nested_items = [
            nested
            for nested in lookup_nested_type_items(
                con,
                item_source,
                function_item,
                exclude_test_symbols=exclude_test_symbols,
            )
            if nested.name != item.name
        ]
        for nested in nested_items:
            if nested.id == item.id:
                continue
            if append_unique_dependency(expanded, nested, seen_ids):
                queue.append((nested, depth + 1))
    return expanded


def collect_function_dependencies(
    con: sqlite3.Connection,
    function_item: CodeItem,
    source: str,
    params: list[CodeItem],
    *,
    expand_nested_types: bool = False,
    max_nesting_depth: int = 4,
    exclude_test_symbols: bool = False,
) -> dict[str, list[CodeItem]]:
    tokens = tokens_from_source(source)
    param_names = {param.name for param in params if param.name}
    member_names = set(
        re.findall(r"(?:->|\.)\s*([A-Za-z_]\w*)\b", strip_comments_and_strings(source))
    )
    call_names = {
        re.split(r"->|\.", re.sub(r"\s+", "", match.group("expr")))[-1]
        for match in CALL_RE.finditer(strip_comments_and_strings(source))
    }
    local_names = param_names | discover_local_names(source) | member_names | call_names
    type_names = set()
    if function_item.type:
        type_names.update(tokens_from_source(function_item.type))
    for param in params:
        if param.type:
            param_type_tokens = tokens_from_source(param.type)
            tokens.update(param_type_tokens)
            type_names.update(param_type_tokens)
    type_names.difference_update(param_names)
    type_names.update(
        re.findall(r"\b(?:struct|union|enum)\s+([A-Za-z_]\w*)\b", strip_comments_and_strings(source))
    )
    dependencies = classify_dependencies(
        lookup_items_for_tokens(
            con,
            tokens,
            function_item,
            source,
            exclude_test_symbols=exclude_test_symbols,
        ),
        local_names,
        type_names,
    )
    if expand_nested_types:
        dependencies = expand_nested_type_dependencies(
            con,
            dependencies,
            function_item,
            max_nesting_depth,
            exclude_test_symbols=exclude_test_symbols,
        )
    return dependencies
def item_to_dict(item: CodeItem, max_snippet_lines: int) -> dict[str, object]:
    data = asdict(item)
    try:
        data["snippet"] = source_slice(item, max_lines=max_snippet_lines)
    except OSError as exc:
        data["snippet_error"] = str(exc)
    return data

