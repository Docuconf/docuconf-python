"""``docuconf`` command line: export a contract from a settings class."""

from __future__ import annotations

import argparse
import difflib
import importlib
import os
import re
import sys
from collections.abc import Sequence
from typing import Any

from pydantic_settings import BaseSettings

from . import _context
from ._version import __version__
from .errors import DeclarationError
from .export import export_warnings, to_contract


def import_settings(target: str) -> type[BaseSettings]:
    """Import ``package.module:ClassName``, with a one-line error (and a suggestion) when that fails.

    While the module is imported, ``docuconf.load()`` and ``Settings()`` skip
    validation and return an unvalidated object, so a module that loads its
    settings at import time can still be exported without an environment.
    """
    module_name, sep, attr = target.partition(":")
    if not sep or not attr or not module_name:
        raise SystemExit(f"docuconf: expected module:Class, such as app.settings:Settings; got {target!r}")
    if os.getcwd() not in sys.path and "" not in sys.path:
        sys.path.insert(0, os.getcwd())
    token = _context.exporting.set(True)
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as e:
        if e.name and module_name.startswith(e.name):
            raise SystemExit(
                f"docuconf: cannot import {target}: no module named {e.name!r} "
                f"(run docuconf from the directory that holds it, or install it)"
            ) from None
        raise SystemExit(f"docuconf: cannot import {target}: {e}") from None
    except Exception as e:
        raise SystemExit(
            f"docuconf: cannot import {target}: {type(e).__name__}: {_first_line(e)}\n"
            f"  run python -c 'import {module_name}' to see the traceback"
        ) from None
    finally:
        _context.exporting.reset(token)
    obj: Any = module
    path = module_name
    for part in attr.split("."):
        if not hasattr(obj, part):
            names = [n for n in dir(obj) if not n.startswith("_")]
            close = difflib.get_close_matches(part, names, n=1)
            hint = f" (did you mean {close[0]!r}?)" if close else ""
            raise SystemExit(f"docuconf: cannot import {target}: {path!r} has no attribute {part!r}{hint}")
        obj = getattr(obj, part)
        path = f"{path}.{part}"
    if not (isinstance(obj, type) and issubclass(obj, BaseSettings)):
        raise SystemExit(f"docuconf: {target} is not a settings class (subclass docuconf.DocuconfSettings)")
    return obj


def _first_line(e: BaseException) -> str:
    return str(e).strip().splitlines()[0] if str(e).strip() else ""


#: The generator version line of an exported contract. ``--check`` ignores it, so upgrading
#: docuconf does not fail every consumer's CI when the contract itself is unchanged.
_GENERATOR_VERSION = re.compile(r'^(\s*version:\s*)"[^"]*"', re.M)


def _without_generator_version(text: str) -> str:
    i = text.find("generator:")
    if i < 0:
        return text
    j = text.find("}", i)
    return text[:i] + _GENERATOR_VERSION.sub(r'\1""', text[i:j]) + text[j:]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docuconf", description="docuconf for pydantic-settings")
    parser.add_argument("--version", action="version", version=f"docuconf-pydantic {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("export", help="export a settings class as a contract.cue")
    ex.add_argument("target", help="the settings class, as module:Class (e.g. app.settings:Settings)")
    ex.add_argument("--out", "-o", help="output file (default: stdout)")
    ex.add_argument(
        "--name",
        help="service name, metadata.name (default: docuconf_service on the class, else the class name in "
        "kebab-case without a Settings or Config suffix; a class named just Settings needs one of the two)",
    )
    ex.add_argument("--app-version", help="metadata.appVersion, e.g. a git SHA")
    ex.add_argument(
        "--check",
        action="store_true",
        help="do not write: fail if --out is missing or differs from the export (the generator version is ignored)",
    )
    args = parser.parse_args(argv)
    if args.check and args.out is None:
        ex.error("--check needs --out FILE, the committed contract to compare with")

    cls = import_settings(args.target)
    try:
        out = to_contract(cls, name=args.name, app_version=args.app_version)
        warnings = export_warnings(cls)
    except DeclarationError as e:
        msg = str(e)
        if args.name is None:
            msg = msg.replace("pass name= or set", "pass --name or set")
        print(msg, file=sys.stderr)
        return 1
    for w in warnings:
        print(f"docuconf: warning: {w}", file=sys.stderr)
    if args.out is None:
        sys.stdout.write(out)
        return 0
    if args.check:
        try:
            with open(args.out, encoding="utf-8") as f:
                current = f.read()
        except FileNotFoundError:
            current = None
        if current is None or _without_generator_version(current) != _without_generator_version(out):
            print(f"docuconf: {args.out} is out of date; run docuconf export without --check", file=sys.stderr)
            return 1
        if current != out:
            print(f"docuconf: note: {args.out} names another docuconf version; re-export to update it", file=sys.stderr)
        return 0
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
