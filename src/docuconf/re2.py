"""RE2 compatibility checks for patterns (SPEC §4.3).

pydantic-core matches ``pattern`` constraints with the Rust ``regex`` crate by
default, which, like RE2, has no lookaround or backreferences, and matches
anywhere in the value (search semantics), as CUE's ``=~`` does. Patterns are
therefore exported as written. This module rejects the Python ``re`` features
RE2 lacks, so a pattern written for ``regex_engine="python-re"`` cannot slip
through.
"""

from __future__ import annotations


def non_re2_feature(pattern: str) -> str | None:
    """Describe the first non-RE2 feature in ``pattern``, or return None."""
    in_class = False
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            nxt = pattern[i + 1] if i + 1 < n else ""
            if not in_class and nxt.isdigit() and nxt != "0":
                return f"backreference \\{nxt}"
            if not in_class and nxt == "Z":
                return "\\Z (RE2 spells end of text \\z; use $ without the m flag)"
            if nxt == "g" and i + 2 < n and pattern[i + 2] == "<":
                return "named group reference \\g<...>"
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
            i += 1
            continue
        if c == "[":
            in_class = True
            i += 1
            if i < n and pattern[i] == "^":
                i += 1
            if i < n and pattern[i] == "]":
                i += 1
            continue
        if c == "(" and pattern[i + 1 : i + 2] == "?":
            rest = pattern[i + 2 : i + 4]
            if rest.startswith("="):
                return "lookahead (?=...)"
            if rest.startswith("!"):
                return "negative lookahead (?!...)"
            if rest == "<=":
                return "lookbehind (?<=...)"
            if rest == "<!":
                return "negative lookbehind (?<!...)"
            if rest.startswith(">"):
                return "atomic group (?>...)"
            if rest.startswith("("):
                return "conditional group (?(...)...)"
            if rest.startswith("P="):
                return "named backreference (?P=...)"
            if rest.startswith("#"):
                return "comment group (?#...)"
        if c in "*+?}" and pattern[i + 1 : i + 2] == "+":
            return f"possessive quantifier {c}+"
        i += 1
    return None
