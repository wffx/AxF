from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .models import CodeItem, KIND


_FILE_ID_CACHE: dict[str, int | None] = {}


def clean_type(value: str | None, name: str | None = None) -> str | None:
    if value is None:
        return None
    marker = name or ""
    cleaned = (
        value.replace("* \x01", "*" + marker)
        .replace("& \x01", "&" + marker)
        .replace("\x01", marker)
        .replace("\x02", "")
    )
    return re.sub(r"\s+", " ", cleaned).strip()


def open_db(db_path: Path) -> sqlite3.Connection:
    uri = "file:" + db_path.as_posix() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def resolve_repo_and_db(repo_arg: str | Path, db_arg: str | Path | None) -> tuple[Path, Path]:
    repo = Path(repo_arg)
    if not repo.is_absolute():
        repo = (Path.cwd() / repo).resolve()

    if db_arg:
        db_path = Path(db_arg)
        if not db_path.is_absolute():
            db_path = (Path.cwd() / db_path).resolve()
        if db_path.is_dir():
            direct = db_path / "BROWSE.VC.DB"
            nested = db_path / ".vscode" / "BROWSE.VC.DB"
            db_path = direct if direct.exists() else nested

        if db_path.parent.name.lower() == ".vscode":
            repo = db_path.parent.parent
        elif db_path.name.upper() == "BROWSE.VC.DB":
            repo = db_path.parent
    else:
        db_path = repo / ".vscode" / "BROWSE.VC.DB"

    if not db_path.exists():
        raise SystemExit(f"SQLite DB not found: {db_path}")
    if not repo.exists():
        raise SystemExit(f"Source root not found: {repo}")
    return repo, db_path


def row_to_item(row: sqlite3.Row) -> CodeItem:
    return CodeItem(
        id=row["id"],
        kind=KIND.get(row["kind"], str(row["kind"])),
        name=row["name"],
        type=clean_type(row["type"], row["name"]),
        attributes=row["attributes"],
        file=row["file_name"],
        start_line=row["start_line"],
        end_line=row["end_line"],
        start_column=row["start_column"],
        end_column=row["end_column"],
        parent_id=row["parent_id"],
    )


def item_query(where: str) -> str:
    return f"""
        select ci.*, f.name as file_name
        from code_items ci
        join files f on f.id = ci.file_id
        where {where}
    """


def find_functions(
    con: sqlite3.Connection, name: str, file_filter: str | None = None
) -> list[CodeItem]:
    params: list[object] = [name]
    where = "ci.kind = 27 and ci.name = ?"
    if file_filter:
        slash_filter = file_filter.replace("\\", "/")
        backslash_filter = file_filter.replace("/", "\\").replace("\\\\", "\\")
        filters = sorted({slash_filter.lower(), backslash_filter.lower()})
        where += " and (" + " or ".join("lower(f.name) like ?" for _ in filters) + ")"
        params.extend("%" + value + "%" for value in filters)
    rows = con.execute(
        item_query(where)
        + " order by (ci.end_line - ci.start_line) desc, ci.start_line",
        params,
    ).fetchall()
    return [row_to_item(row) for row in rows]


def function_params(con: sqlite3.Connection, function_id: int) -> list[CodeItem]:
    rows = con.execute(
        item_query("ci.parent_id = ? and ci.kind = 9")
        + " order by ci.param_number, ci.start_line, ci.start_column",
        (function_id,),
    ).fetchall()
    return [row_to_item(row) for row in rows]
def resolve_function(
    con: sqlite3.Connection, name: str, caller_file: str | None = None
) -> CodeItem | None:
    rows = con.execute(
        item_query("ci.kind = 27 and ci.name = ?")
        + " order by (ci.end_line - ci.start_line) desc",
        (name,),
    ).fetchall()
    if not rows:
        return None
    items = [row_to_item(row) for row in rows]
    caller_lower = (caller_file or "").lower()

    def score(candidate: CodeItem) -> tuple[int, int]:
        path = candidate.file.lower()
        value = candidate.span
        if caller_lower and path == caller_lower:
            value += 1000
        caller_is_tools = "\\tools\\" in caller_lower or "/tools/" in caller_lower
        candidate_is_tools = "\\tools\\" in path or "/tools/" in path
        if caller_lower and not caller_is_tools:
            value += -300 if candidate_is_tools else 300
        if candidate.attributes & 2:
            value += 100
        if candidate.span == 0:
            value -= 100
        return (value, candidate.span)

    return max(items, key=score)
def find_file_id(con: sqlite3.Connection, file_path: str) -> int | None:
    normalized = str(Path(file_path).resolve()).upper()
    if normalized in _FILE_ID_CACHE:
        return _FILE_ID_CACHE[normalized]
    row = con.execute("select id from files where name = ?", (normalized,)).fetchone()
    file_id = int(row["id"]) if row else None
    _FILE_ID_CACHE[normalized] = file_id
    return file_id


def find_enclosing_function(con: sqlite3.Connection, file_path: str, line: int) -> CodeItem | None:
    file_id = find_file_id(con, file_path)
    if file_id is None:
        return None
    rows = con.execute(
        item_query("ci.kind = 27 and ci.file_id = ? and ci.start_line <= ? and ci.end_line >= ?")
        + " order by (ci.end_line - ci.start_line) asc limit 1",
        (file_id, line, line),
    ).fetchall()
    if not rows:
        return None
    return row_to_item(rows[0])
