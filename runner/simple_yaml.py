from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class SimpleYamlError(ValueError):
    pass


def load_simple_yaml(path: Path) -> dict[str, Any]:
    return parse_simple_yaml(path.read_text(encoding="utf-8"))


def parse_simple_yaml(text: str) -> dict[str, Any]:
    lines = _preprocess(text)
    if not lines:
        return {}
    value, index = _parse_block(lines, 0, 0)
    if index != len(lines):
        raise SimpleYamlError(f"unexpected trailing YAML content: {lines[index][1]}")
    if not isinstance(value, dict):
        raise SimpleYamlError("top-level YAML value must be a mapping")
    return value


def _preprocess(text: str) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        content = _strip_comment(line.rstrip())
        if not content.strip():
            continue
        indent = len(content) - len(content.lstrip(" "))
        if indent % 2 != 0:
            raise SimpleYamlError("only even-space indentation is supported")
        result.append((indent, content.strip()))
    return result


def _strip_comment(line: str) -> str:
    in_single = False
    in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            before = line[index - 1] if index else " "
            if before.isspace():
                return line[:index].rstrip()
    return line


def _parse_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines):
        return {}, index
    current_indent, content = lines[index]
    if current_indent < indent:
        return {}, index
    if current_indent != indent:
        raise SimpleYamlError(f"unexpected indentation for: {content}")
    if content.startswith("- "):
        return _parse_list(lines, index, indent)
    return _parse_map(lines, index, indent)


def _parse_map(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[dict[str, Any], int]:
    data: dict[str, Any] = {}
    while index < len(lines):
        current_indent, content = lines[index]
        if current_indent < indent:
            break
        if current_indent != indent:
            raise SimpleYamlError(f"unexpected indentation for: {content}")
        if content.startswith("- "):
            break
        key, raw_value = _split_key_value(content)
        index += 1
        if raw_value == "":
            if index < len(lines) and lines[index][0] > indent:
                value, index = _parse_block(lines, index, indent + 2)
            else:
                value = {}
        else:
            value = _parse_scalar(raw_value)
        data[key] = value
    return data, index


def _parse_list(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    while index < len(lines):
        current_indent, content = lines[index]
        if current_indent < indent:
            break
        if current_indent != indent or not content.startswith("- "):
            break
        item_text = content[2:].strip()
        index += 1
        if not item_text:
            item, index = _parse_block(lines, index, indent + 2)
            items.append(item)
            continue
        if ":" in item_text:
            key, raw_value = _split_key_value(item_text)
            item_map: dict[str, Any] = {}
            if raw_value == "":
                value, index = _parse_block(lines, index, indent + 2)
            else:
                value = _parse_scalar(raw_value)
            item_map[key] = value
            while index < len(lines) and lines[index][0] == indent + 2 and not lines[index][1].startswith("- "):
                child_key, child_raw = _split_key_value(lines[index][1])
                index += 1
                if child_raw == "":
                    if index < len(lines) and lines[index][0] > indent + 2:
                        child_value, index = _parse_block(lines, index, indent + 4)
                    else:
                        child_value = {}
                else:
                    child_value = _parse_scalar(child_raw)
                item_map[child_key] = child_value
            items.append(item_map)
        else:
            items.append(_parse_scalar(item_text))
    return items, index


def _split_key_value(content: str) -> tuple[str, str]:
    if ":" not in content:
        raise SimpleYamlError(f"expected key: value, got: {content}")
    key, value = content.split(":", 1)
    key = key.strip()
    if not key:
        raise SimpleYamlError(f"empty key in: {content}")
    return key, value.strip()


def _parse_scalar(value: str) -> Any:
    if value in {"null", "None", "~"}:
        return None
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in _split_inline_list(inner)]
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _split_inline_list(inner: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    for char in inner:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "," and not in_single and not in_double:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    parts.append("".join(current).strip())
    return parts
