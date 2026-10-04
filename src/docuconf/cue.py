"""A small CUE writer for plain data (SPEC §4: contracts are data).

Output follows ``cue fmt``: tabs, aligned values for runs of single-line
fields, lists of scalars inline.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_KEYWORDS = {"package", "import", "for", "in", "if", "let", "true", "false", "null", "_", "__"}


def label(key: str) -> str:
    return key if _IDENT.match(key) and key not in _KEYWORDS else _string(key)


def _string(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _is_scalar(v: Any) -> bool:
    return v is None or isinstance(v, (str, int, float, bool))


def _scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError(f"cannot write {v} in CUE")
        if v == int(v) and abs(v) < 1e16:
            return f"{int(v)}.0"
        return repr(v)
    if isinstance(v, str):
        return _string(v)
    raise TypeError(f"cannot write {type(v).__name__} in CUE")


def value(v: Any, indent: str) -> str:
    if _is_scalar(v):
        return _scalar(v)
    if isinstance(v, (list, tuple)):
        if not v:
            return "[]"
        if all(_is_scalar(x) for x in v):
            return "[" + ", ".join(_scalar(x) for x in v) + "]"
        inner = indent + "\t"
        return "[\n" + "\n".join(f"{inner}{value(x, inner)}," for x in v) + f"\n{indent}]"
    if isinstance(v, Mapping):
        if not v:
            return "{}"
        return "{\n" + fields(list(v.items()), indent + "\t") + f"\n{indent}}}"
    raise TypeError(f"cannot write {type(v).__name__} in CUE")


def fields(entries: list[tuple[str, Any]], indent: str) -> str:
    """Write struct fields; runs of scalar fields are aligned as ``cue fmt`` does."""
    lines: list[str] = []
    run: list[tuple[str, str]] = []

    def flush() -> None:
        width = max(len(k) for k, _ in run)
        for k, s in run:
            lines.append(f"{indent}{k}:{' ' * (width - len(k) + 1)}{s}")
        run.clear()

    for k, v in entries:
        lab = label(k)
        if _is_scalar(v):
            run.append((lab, _scalar(v)))
            continue
        if run:
            flush()
        lines.append(f"{indent}{lab}: {value(v, indent)}")
    if run:
        flush()
    return "\n".join(lines)
