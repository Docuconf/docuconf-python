"""Contract-first mode (SPEC §11.2 item 11) and the Duration marker."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

import pytest
from pydantic import Field

import docuconf
from docuconf import ConfigValidationError, DeclarationError, DocuconfSettings, Duration

CONTRACT: dict[str, Any] = {
    "apiVersion": "docuconf.dev/v1alpha1",
    "kind": "ConfigContract",
    "metadata": {"name": "orders", "generator": {"language": "go", "sdk": "docuconf-go", "version": "0.1.0"}},
    "vars": {
        "PORT": {"type": "int", "description": "HTTP listen port", "min": 1, "max": 65535, "default": 8080},
        "TIMEOUT": {"type": "duration", "description": "Request timeout", "encoding": "go", "default": "30s"},
        "PARTITIONS": {
            "type": "list",
            "description": "Partitions this instance consumes",
            "items": "int",
            "encoding": "indexed",
            "itemMin": 0,
            "itemMax": 2147483647,
        },
        "API_TOKEN": {"type": "string", "description": "Token for the API", "secret": True, "required": True},
    },
}


def test_load_contract_from_mapping_text_and_path(tmp_path: Path) -> None:
    env = {"API_TOKEN": "tok", "TIMEOUT": "1m30s", "PARTITIONS__0": "3", "PARTITIONS__1": "7"}
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(CONTRACT))
    for source in (CONTRACT, json.dumps(CONTRACT), path, str(path)):
        values = docuconf.load_contract(source, env, termination_log=False)
        assert values.PORT == 8080  # type: ignore[attr-defined]
        assert timedelta(seconds=90) == values.TIMEOUT  # type: ignore[attr-defined]
        assert values.PARTITIONS == [3, 7]  # type: ignore[attr-defined]
        assert values.model_dump()["API_TOKEN"] == "tok"


def test_load_contract_reports_every_violation(tmp_path: Path) -> None:
    log = tmp_path / "log"
    env = {"PORT": "0", "TIMEOUT": "PT90S", "PARTITIONS__0": "-1"}
    with pytest.raises(ConfigValidationError) as info:
        docuconf.load_contract(CONTRACT, env, termination_log=str(log))
    assert sorted((v.input, v.code) for v in info.value.violations) == [
        ("API_TOKEN", "missing_required"),
        ("PARTITIONS", "out_of_range"),
        ("PORT", "out_of_range"),
        ("TIMEOUT", "invalid_type"),
    ]
    assert "PORT [out_of_range]" in log.read_text()


def test_load_contract_reads_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_TOKEN", "from-env")
    monkeypatch.setenv("PORT", "9090")
    values = docuconf.load_contract(CONTRACT, termination_log=False)
    assert (values.PORT, values.API_TOKEN) == (9090, "from-env")  # type: ignore[attr-defined]


def test_the_contract_round_trips() -> None:
    # The generated class exports the contract it was built from.
    cls = docuconf.contract_settings(CONTRACT)
    exported = docuconf.contract_data(cls)["vars"]
    for name, var in CONTRACT["vars"].items():
        assert exported[name] == var, name


def test_contracts_the_mode_cannot_load() -> None:
    bad = {**CONTRACT, "files": {"tls": {"type": "tls", "description": "Serving key pair", "path": "/etc/tls"}}}
    with pytest.raises(DeclarationError, match="variables only"):
        docuconf.load_contract(bad, {})
    bad = {**CONTRACT, "vars": {"X": {"type": "decimal", "description": "Not a type"}}}
    with pytest.raises(DeclarationError, match="unknown type"):
        docuconf.load_contract(bad, {})
    bad = {**CONTRACT, "vars": {"X": {"type": "string", "description": "Lookahead", "pattern": "a(?=b)"}}}
    with pytest.raises(DeclarationError, match="RE2"):
        docuconf.load_contract(bad, {})
    with pytest.raises(DeclarationError, match="not a contract"):
        docuconf.load_contract({"vars": {}}, {})


@pytest.mark.parametrize(
    ("encoding", "raw", "want"),
    [
        ("go", "1h2m3.5s", timedelta(hours=1, minutes=2, seconds=3.5)),
        ("seconds", "0.25", timedelta(milliseconds=250)),
        ("timespan", "1.02:03:04.5", timedelta(days=1, hours=2, minutes=3, seconds=4.5)),
        ("iso8601", "PT1.5S", timedelta(seconds=1.5)),
    ],
)
def test_duration_marker(monkeypatch: pytest.MonkeyPatch, encoding: Any, raw: str, want: timedelta) -> None:
    class S(DocuconfSettings):
        timeout: Annotated[timedelta, Duration(encoding)] = Field(timedelta(seconds=30), description="Timeout")

    assert docuconf.contract_data(S, name="svc")["vars"]["TIMEOUT"] == {
        "type": "duration",
        "description": "Timeout",
        "encoding": encoding,
        "default": "30s",
    }
    monkeypatch.setenv("TIMEOUT", raw)
    assert docuconf.load(S, watch=False).timeout == want
    monkeypatch.setenv("TIMEOUT", "garbage")
    with pytest.raises(ConfigValidationError) as info:
        docuconf.load(S, watch=False, termination_log=False)
    assert info.value.codes == ["invalid_type"]


@pytest.mark.parametrize(
    ("env", "missing"),
    [({"PARTITIONS__0": "1", "PARTITIONS__2": "3"}, "PARTITIONS__1"), ({"PARTITIONS__1": "2"}, "PARTITIONS__0")],
)
def test_indexed_list_gaps(env: dict[str, str], missing: str) -> None:
    with pytest.raises(ConfigValidationError) as info:
        docuconf.load_contract(CONTRACT, {"API_TOKEN": "tok", **env}, termination_log=False)
    assert [(v.input, v.code) for v in info.value.violations] == [("PARTITIONS", "invalid_type")]
    assert f"{missing} is not set" in str(info.value)


def test_indexed_list_ignores_other_suffixes_and_the_bare_name() -> None:
    env = {"API_TOKEN": "tok", "PARTITIONS": "[9]", "PARTITIONS__X": "9", "PARTITIONS__00": "9"}
    assert docuconf.load_contract(CONTRACT, env, termination_log=False).PARTITIONS is None  # type: ignore[attr-defined]
