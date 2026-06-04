#!/usr/bin/env bash
set -euo pipefail

export PYTHONPATH="/workspace:/workspace/knowledge_base:${PYTHONPATH:-}"

link_index_source_root() {
  export AXF_CONTAINER_SOURCE_ROOT="${AXF_CONTAINER_SOURCE_ROOT:-/linux-7.0}"
  export AXF_INDEX_DB="${AXF_INDEX_DB:-${AXF_CONTAINER_SOURCE_ROOT}/.vscode/BROWSE.VC.DB}"
  python - <<'PY'
import os
import sqlite3
import sys
from pathlib import Path

source_root = Path(os.environ.get("AXF_CONTAINER_SOURCE_ROOT", "/linux-7.0"))
db_path = Path(os.environ.get("AXF_INDEX_DB", str(source_root / ".vscode" / "BROWSE.VC.DB")))
if not source_root.exists() or not db_path.exists():
    raise SystemExit(0)


def link_indexed_root(indexed_root: Path) -> None:
    if indexed_root == source_root or indexed_root.exists():
        return
    try:
        indexed_root.parent.mkdir(parents=True, exist_ok=True)
        indexed_root.symlink_to(source_root, target_is_directory=True)
        print(f"AxF Docker: linked indexed source root {indexed_root} -> {source_root}", file=sys.stderr)
    except OSError as exc:
        print(f"AxF Docker: cannot link indexed source root {indexed_root}: {exc}", file=sys.stderr)


def indexed_root_from_path(path_text: str) -> Path | None:
    normalized = path_text.replace("\\", "/")
    if not normalized.startswith("/") or not source_root.name:
        return None
    marker = f"/{source_root.name}/"
    position = normalized.lower().find(marker.lower())
    if position < 0:
        return None
    root_text = normalized[: position + 1 + len(source_root.name)]
    if not root_text:
        return None
    return Path(root_text)


try:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
except sqlite3.Error as exc:
    print(f"AxF Docker: cannot inspect index DB {db_path}: {exc}", file=sys.stderr)
    raise SystemExit(0)

try:
    pattern = f"%/{source_root.name.lower()}/%"
    rows = con.execute(
        "select name from files where lower(replace(name, '\\\\', '/')) like ? limit 100",
        (pattern,),
    )
    for (name,) in rows:
        indexed_root = indexed_root_from_path(str(name))
        if indexed_root is not None:
            link_indexed_root(indexed_root)
            break
finally:
    con.close()
PY
}

link_index_source_root

exec "$@"
