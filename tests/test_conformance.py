"""The shared conformance suite (SPEC §12), run through the contract-first mode.

The cases live in docuconf-go (``conformance/cases.json``). Point
``DOCUCONF_CONFORMANCE`` at that file, or check docuconf-go out next to this
repository; ``DOCUCONF_REQUIRE_CONFORMANCE=1`` turns a missing file, or a
skipped case, into a failure.

The runner keeps an allow-list of the capability tags this SDK supports
(conformance/README.md). A case is skipped only when it requires a tag that
is not on it, which includes a tag the runner does not know, so a new tag
never runs against an SDK written before it.
"""

from __future__ import annotations

import base64
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

_HAS_JSONSCHEMA = importlib.util.find_spec("jsonschema") is not None
_HAS_CRYPTOGRAPHY = importlib.util.find_spec("cryptography") is not None
_HAS_YAML = importlib.util.find_spec("yaml") is not None

#: The capability tags (conformance/README.md) this SDK supports: an allow-list, never a deny-list.
#: json-schema needs the jsonschema extra; files needs it too (config files are checked against their
#: schema), the tls extra (certificates and PKCS#12 keystores) and the yaml extra. CI installs all three.
SUPPORTED = {"int64", "key-set", "deprecated", "strict-parsing", "profiles", "overlays"}
if _HAS_JSONSCHEMA:
    SUPPORTED.add("json-schema")
if _HAS_JSONSCHEMA and _HAS_CRYPTOGRAPHY and _HAS_YAML:
    SUPPORTED.add("files")


def _cases() -> list[dict[str, Any]]:
    if not CASES.is_file():
        return []
    data = json.loads(CASES.read_text(encoding="utf-8"))
    assert data.get("version") == 1, f"{CASES}: unsupported cases.json version {data.get('version')!r}"
    cases: list[dict[str, Any]] = data["cases"]
    return cases


ALL = _cases()


def _unsupported(case: dict[str, Any]) -> list[str]:
    return sorted(set(case.get("requires", [])) - SUPPORTED)


def test_cases_file() -> None:
    if not CASES.is_file():
        if REQUIRED:
            pytest.fail(f"DOCUCONF_REQUIRE_CONFORMANCE=1, but {CASES} does not exist")
        pytest.skip(f"{CASES} not found; set DOCUCONF_CONFORMANCE")
    assert ALL, f"{CASES} holds no cases"


def test_nothing_skipped() -> None:
    """With DOCUCONF_REQUIRE_CONFORMANCE=1 (CI), every case runs: a skipped case fails the suite."""
    skipped = {c["id"]: _unsupported(c) for c in ALL if _unsupported(c)}
    if skipped and REQUIRED:
        lines = "\n".join(f"  {cid}: requires {', '.join(tags)}" for cid, tags in sorted(skipped.items()))
        pytest.fail(f"{len(skipped)} of {len(ALL)} conformance cases would be skipped:\n{lines}")
    if skipped:
        pytest.skip(f"{len(skipped)} of {len(ALL)} cases skipped (unsupported tags)")


def _json(value: Any) -> Any:
    """A typed value as the JSON the case expects."""
    if isinstance(value, timedelta):
        return to_go(value, signed=True)
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, docuconf.KeySet):
        return [k.get_secret_value() for k in value.keys]
    if isinstance(value, (docuconf.TlsKeyPair, docuconf.CaBundle, docuconf.Keystore, bytes, Path)):
        return True  # a file input other than config and text: present
    if isinstance(value, list):
        return [_json(v) for v in value]
    return value


def _write_files(root: Path, files: dict[str, dict[str, str]]) -> None:
    """Each file of the case under ``root``, at its absolute path."""
    for path, content in files.items():
        target = root / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        if "base64" in content:
            target.write_bytes(base64.b64decode(content["base64"]))
        else:
            target.write_bytes(content["text"].encode("utf-8"))


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
    missing = _unsupported(case)
    if missing:
        pytest.skip(f"requires {', '.join(missing)}")
    log = tmp_path / "termination-log"
    # A new, empty file root for every case, files or not, so no case reads the machine's own files.
    root = tmp_path / "root"
    root.mkdir()
    _write_files(root, case.get("files") or {})
    env: dict[str, str] = case["env"]
    try:
        values = docuconf.load_contract(
            case["contract"], {**env, "DOCUCONF_FILE_ROOT": str(root)}, termination_log=str(log)
        )
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
        got = _json(values.value(name))
        assert _same(got, want), f"{case['id']}: {name} is {got!r}, want {want!r}"
