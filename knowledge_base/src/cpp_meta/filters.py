from __future__ import annotations

import re

from .models import CodeItem


TEST_SYMBOL_PATH_COMPONENTS = {
    "dt",
    "st",
    "test",
    "tests",
    "testing",
    "selftest",
    "selftests",
    "unit_test",
    "unit_tests",
    "unittest",
    "unittests",
}


def path_components(path_text: str) -> list[str]:
    return [part for part in re.split(r"[\\/]+", path_text.lower()) if part]


def is_test_symbol_path(path_text: str) -> bool:
    parts = path_components(path_text)
    for part in parts:
        if part in TEST_SYMBOL_PATH_COMPONENTS:
            return True
        if part.endswith("_test") or part.endswith("_tests"):
            return True
        if part.startswith("test_") or part.startswith("tests_"):
            return True
    return False


def exclude_test_symbol_items(items: list[CodeItem]) -> list[CodeItem]:
    return [item for item in items if not is_test_symbol_path(item.file)]
