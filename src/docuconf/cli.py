"""``docuconf`` command line: export a contract from a settings class."""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from collections.abc import Sequence
from typing import Any

from pydantic_settings import BaseSettings

from ._version import __version__
from .errors import DeclarationError
from .export import export_warnings, to_contract


def import_settings(target: str) -> type[BaseSettings]:
    """Import ``package.module:ClassName``."""
    module_name, sep, attr = target.partition(":")
    if not sep or not attr:
        raise SystemExit(f"docuconf: expected module:Class, got {target!r}")
    if os.getcwd() not in sys.path and "" not in sys.path:
        sys.path.insert(0, os.getcwd())
    module = importlib.import_module(module_name)
    obj: Any = module
    for part in attr.split("."):
        obj = getattr(obj, part)
    if not (isinstance(obj, type) and issubclass(obj, BaseSettings)):
        raise SystemExit(f"docuconf: {target} is not a pydantic-settings BaseSettings class")
    return obj


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docuconf", description="docuconf for pydantic-settings")
    parser.add_argument("--version", action="version", version=f"docuconf-pydantic {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("export", help="export a settings class as a contract.cue")
    ex.add_argument("target", help="the settings class, as module:Class (e.g. app.settings:Settings)")
    ex.add_argument("--out", "-o", help="output file (default: stdout)")
    ex.add_argument("--name", help="service name (default: docuconf_service on the class, or the class name)")
    ex.add_argument("--app-version", help="metadata.appVersion, e.g. a git SHA")
    ex.add_argument("--check", action="store_true", help="fail if --out exists and differs from the export")
    args = parser.parse_args(argv)

    cls = import_settings(args.target)
    try:
        out = to_contract(cls, name=args.name, app_version=args.app_version)
        warnings = export_warnings(cls)
    except DeclarationError as e:
        print(e, file=sys.stderr)
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
        if current != out:
            print(f"docuconf: {args.out} is out of date; run docuconf export without --check", file=sys.stderr)
            return 1
        return 0
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
