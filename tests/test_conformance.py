"""The shared conformance suite (SPEC §12), run through the contract-first mode.

The cases live in docuconf-go (``conformance/cases.json``). Point
``DOCUCONF_CONFORMANCE`` at that file, or check docuconf-go out next to this
repository; ``DOCUCONF_REQUIRE_CONFORMANCE=1`` turns a missing file into a
failure instead of a skip.
"""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

import docuconf
from docuconf.durations import to_go

HERE = Path(__file__).parent
CASES = Path(os.environ.get("DOCUCONF_CONFORMANCE") or HERE.parent.parent / "docuconf-go/conformance/cases.json")
REQUIRED = os.environ.get("DOCUCONF_REQUIRE_CONFORMANCE") == "1"

#: Capability tags (conformance/README.md) this SDK supports. json-schema needs the jsonschema extra.
SUPPORTED = {"int64"} | ({"json-schema"} if importlib.util.find_spec("jsonschema") else set())


def _cases() -> list[dict[str, Any]]:
    if not CASES.is_file():
        return []
    data = json.loads(CASES.read_text(encoding="utf-8"))
    assert data.get("version") == 1, f"{CASES}: unsupported cases.json version {data.get('version')!r}"
    cases: list[dict[str, Any]] = data["cases"]
    return cases


ALL = _cases()


def test_cases_file() -> None:
    if not CASES.is_file():
        if REQUIRED:
            pytest.fail(f"DOCUCONF_REQUIRE_CONFORMANCE=1, but {CASES} does not exist")
        pytest.skip(f"{CASES} not found; set DOCUCONF_CONFORMANCE")
    assert ALL, f"{CASES} holds no cases"


def _json(value: Any) -> Any:
    """A typed value as the JSON the case expects."""
    if isinstance(value, timedelta):
        return to_go(value)
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, list):
        return [_json(v) for v in value]
    return value


def _same(got: Any, want: Any) -> bool:
    """JSON equality: integers exactly, floats numerically, bools only equal bools."""
    if isinstance(want, bool) or isinstance(got, bool):
        return type(got) is type(want) and got == want
    if isinstance(want, (int, float)) and isinstance(got, (int, float)):
        if isinstance(want, int) and isinstance(got, int):
            return got == want
        return float(got) == float(want)
    if isinstance(want, list):
        return isinstance(got, list) and len(got) == len(want) and all(map(_same, got, want))
    if isinstance(want, dict):
        return isinstance(got, dict) and got.keys() == want.keys() and all(_same(got[k], want[k]) for k in want)
    return type(got) is type(want) and got == want


@pytest.mark.parametrize("case", ALL, ids=[c["id"] for c in ALL])
def test_case(case: dict[str, Any], tmp_path: Path) -> None:
    missing = sorted(set(case.get("requires", [])) - SUPPORTED)
    if missing:
        pytest.skip(f"requires {', '.join(missing)}")
    log = tmp_path / "termination-log"
    env: dict[str, str] = case["env"]
    try:
        values = docuconf.load_contract(case["contract"], env, termination_log=str(log))
    except docuconf.ConfigValidationError as e:
        if "errors" not in case:
            pytest.fail(f"{case['id']}: want values, got errors:\n{e}")
        got = sorted({(v.input, v.code) for v in e.violations})
        want = sorted({(x["var"], x["code"]) for x in case["errors"]})
        assert got == want, f"{case['id']}: errors differ"
        output = str(e) + "\n" + "\n".join(v.message for v in e.violations) + "\n" + log.read_text()
        for name, var in case["contract"]["vars"].items():
            raw = env.get(name)
            if var.get("secret") and raw:
                assert raw not in output, f"{case['id']}: the value of secret {name} appears in the error output"
        return
    if "expect" not in case:
        pytest.fail(f"{case['id']}: want errors {case['errors']}, got values")
    for name, want in case["expect"].items():
        got = _json(getattr(values, name))
        assert _same(got, want), f"{case['id']}: {name} is {got!r}, want {want!r}"
