"""Where an input's ``description`` and ``details`` come from (SPEC §4.2, §14.7).

``description`` is ``Field(description=...)``. ``details`` is
``Field(json_schema_extra={"details": ...})`` or the field's attribute
docstring, the string literal written on the line after it::

    class Settings(DocuconfSettings):
        port: int = Field(8080, description="HTTP listen port")
        '''Behind the mesh, keep the default.'''

A field with no ``Field(description=...)`` takes its description from the
docstring's first paragraph, and its details from the rest, as Go and Rust
doc comments do. So does a class with ``use_attribute_docstrings=True``,
where pydantic copies the whole docstring into the description.

Docstrings are reStructuredText by convention; ``details`` is CommonMark, so
Sphinx roles (``:class:`Foo```), double-backquoted literals and ``::`` /
``.. code-block::`` literal blocks are converted, and field lists
(``:param x:``) dropped.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from functools import lru_cache
from itertools import pairwise
from typing import Any

from pydantic.fields import FieldInfo

#: The most characters (Unicode code points) ``details`` may have (SPEC §4.2).
MAX_DETAILS = 4000


@lru_cache(maxsize=256)
def _own_docstrings(cls: type) -> dict[str, str]:
    try:
        source = textwrap.dedent(inspect.getsource(cls))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError, IndentationError):
        # No source: a class built at runtime (create_model, contract-first mode) or a frozen app.
        return {}
    node = tree.body[0] if tree.body else None
    if not isinstance(node, ast.ClassDef):
        return {}
    out: dict[str, str] = {}
    for stmt, after in pairwise(node.body):
        if (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and isinstance(after, ast.Expr)
            and isinstance(after.value, ast.Constant)
            and isinstance(after.value.value, str)
        ):
            out[stmt.target.id] = inspect.cleandoc(after.value.value)
    return out


def attribute_docstring(cls: type, name: str) -> str | None:
    """The docstring of field ``name``, from the class in ``cls``'s MRO that annotates it."""
    for c in cls.__mro__:
        try:
            own = inspect.get_annotations(c)
        except Exception:
            continue
        if name in own:
            try:
                return _own_docstrings(c).get(name)
            except TypeError:  # an unhashable class
                return None
    return None


_BLANK_LINES = re.compile(r"\n[ \t]*\n")


def split_doc(text: str) -> tuple[str, str]:
    """The first paragraph of a docstring, on one line without a final period, and the rest."""
    text = inspect.cleandoc(text).strip()
    if not text:
        return "", ""
    parts = _BLANK_LINES.split(text, maxsplit=1)
    first = " ".join(parts[0].split())
    if first.endswith(".") and not first.endswith(".."):
        first = first[:-1]
    rest = parts[1].strip("\n") if len(parts) > 1 else ""
    return first, rest


# :role:`text`, :py:role:`text` and :role:`title <target>`.
_ROLE = re.compile(r":(?:[a-z]+:)?[a-z]+:`(?:~|!)?([^`<]*?)(?:\s*<([^`>]+)>)?`")
_LITERAL = re.compile(r"``([^`]+?)``")
_DIRECTIVE = re.compile(r"^(\s*)\.\.\s+(code-block|code|sourcecode)::\s*(\S*)\s*$")
_FIELD = re.compile(
    r"^:(param|parameter|arg|argument|key|keyword|type|raises|raise|except|var|ivar|cvar|vartype"
    r"|returns|return|rtype|meta)\b[^:]*:"
)


def to_markdown(text: str) -> str:
    """Convert a reStructuredText docstring body to CommonMark (SPEC §14.7)."""
    lines = inspect.cleandoc(text).splitlines()
    out: list[str] = []
    i = 0
    in_fence = False
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence:
            out.append(line)
            i += 1
            continue
        m = _DIRECTIVE.match(line)
        literal_lang: str | None = None
        if m:
            literal_lang = m.group(3)
            i += 1
            # Directive options (":linenos:") belong to the directive.
            while i < len(lines) and lines[i].strip().startswith(":"):
                i += 1
        elif stripped.endswith("::") and not stripped.startswith(".."):
            # "Example::" introduces a literal block: keep "Example:"; a bare "::" goes away.
            if stripped != "::":
                out.append(_inline(line.rstrip()[:-1]))
            literal_lang = ""
            i += 1
        if literal_lang is not None:
            block, i = _indented_block(lines, i)
            if block:
                if out and out[-1].strip():
                    out.append("")
                out.append("```" + literal_lang)
                out.extend(block)
                out.append("```")
            continue
        if _FIELD.match(stripped):
            # A field list (":param x: ..."): API documentation, not configuration docs. Drop it with its
            # continuation lines.
            i += 1
            while i < len(lines) and lines[i].startswith((" ", "\t")) and lines[i].strip():
                i += 1
            continue
        out.append(_inline(line))
        i += 1
    return "\n".join(out).strip("\n")


def _indented_block(lines: list[str], i: int) -> tuple[list[str], int]:
    """The indented block starting at ``lines[i]`` (after blank lines), dedented, and the index after it."""
    while i < len(lines) and not lines[i].strip():
        i += 1
    start = i
    while i < len(lines) and (not lines[i].strip() or lines[i][:1] in (" ", "\t")):
        i += 1
    end = i
    while end > start and not lines[end - 1].strip():
        end -= 1
    block = textwrap.dedent("\n".join(lines[start:end])).splitlines()
    # Keep the blank line that ended the block, so the next paragraph stays separate.
    return block, end


def _inline(line: str) -> str:
    line = _LITERAL.sub(r"`\1`", line)
    return _ROLE.sub(lambda m: f"`{(m.group(1) or m.group(2) or '').strip()}`", line)


def describe(fi: FieldInfo, docstring: str | None) -> tuple[str, str | None]:
    """The input's description and details (``None`` when it has none), before validation."""
    explicit = (fi.description or "").strip()
    doc = inspect.cleandoc(docstring).strip() if docstring else ""
    if explicit and doc and " ".join(explicit.split()) == " ".join(doc.split()):
        # use_attribute_docstrings=True: pydantic copied the docstring into the description.
        explicit = ""
    extra = fi.json_schema_extra if isinstance(fi.json_schema_extra, dict) else {}
    given: Any = extra.get("details")
    if explicit:
        description, rest = explicit, doc
    else:
        description, rest = split_doc(doc)
    if given is not None:
        return description, str(given)
    return description, (to_markdown(rest) or None) if rest else None


def check(description: str, details: str | None, problem: Any) -> None:
    """Report a missing or short description and blank or too-long details (SPEC §4.2)."""
    if len(description) < 5:
        problem(
            "needs a description of at least 5 characters: "
            'Field(description="..."), or an attribute docstring after the field'
        )
    if details is None:
        return
    if not details.strip():
        problem("details must not be blank")
    elif len(details) > MAX_DETAILS:
        problem(f"details are {len(details)} characters; at most {MAX_DETAILS} are allowed")
