from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILTIN_KNOWLEDGE_ROOT = PROJECT_ROOT / "knowledge_base"


@dataclass(frozen=True)
class KRepoConfig:
    provider: str
    root: Path
    query_script: Path
    python: str = sys.executable
    fallback_to_builtin: bool = False


def resolve_krepo_config(
    krepo_root: str | Path | None = None,
    *,
    provider: str | None = None,
    python: str | None = None,
) -> KRepoConfig:
    selected_provider = (provider or "").strip().lower()
    if selected_provider not in {"", "builtin", "external"}:
        raise ValueError(f"unknown kRepo provider: {provider}")

    explicit_root = Path(krepo_root).expanduser() if krepo_root else None
    env_root = Path(os.environ["KREPO_ROOT"]).expanduser() if os.environ.get("KREPO_ROOT") else None
    python_bin = python or sys.executable

    if selected_provider == "builtin":
        return _builtin_config(python_bin)

    if selected_provider == "external":
        root = explicit_root or env_root
        if not root:
            raise ValueError("external kRepo provider requires --krepo or KREPO_ROOT")
        return _external_config(root, python_bin)

    if explicit_root:
        return _external_config(explicit_root, python_bin)

    if env_root:
        query_script = _query_script_for_root(env_root)
        if query_script.exists():
            return KRepoConfig("external", env_root.resolve(), query_script.resolve(), python_bin, fallback_to_builtin=True)

    return _builtin_config(python_bin)


def _builtin_config(python_bin: str) -> KRepoConfig:
    query_script = BUILTIN_KNOWLEDGE_ROOT / "src" / "cpp_meta_query.py"
    if not query_script.exists():
        raise ValueError(f"built-in knowledge_base query script not found: {query_script}")
    return KRepoConfig("builtin", BUILTIN_KNOWLEDGE_ROOT, query_script, python_bin)


def _external_config(root: Path, python_bin: str) -> KRepoConfig:
    query_script = _query_script_for_root(root)
    if not query_script.exists():
        raise ValueError(f"kRepo query script not found under {root}")
    return KRepoConfig("external", root.resolve(), query_script.resolve(), python_bin)


def _query_script_for_root(root: Path) -> Path:
    root = root.expanduser()
    candidates = [
        root / "src" / "cpp_meta_query.py",
        root / "knowledge_base" / "src" / "cpp_meta_query.py",
        root / "cpp_meta_query.py",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]
