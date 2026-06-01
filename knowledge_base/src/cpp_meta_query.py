#!/usr/bin/env python3
"""Preferred CLI wrapper for the C/C++ metadata query package."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.cpp_meta import (
    analyze_function,
    export_source_bundle,
    export_subfunction_source_bundle,
    lookup_symbol_source,
    print_function_call_sequence,
    print_function_param_constraints,
    print_symbol_source,
)
from src.cpp_meta.cli import main


__all__ = [
    "analyze_function",
    "export_source_bundle",
    "export_subfunction_source_bundle",
    "lookup_symbol_source",
    "print_function_call_sequence",
    "print_function_param_constraints",
    "print_symbol_source",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
