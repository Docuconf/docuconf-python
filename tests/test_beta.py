"""The beta features in declaration mode: key sets, deprecated inputs and strict parsing (SPEC §4.2, §4.3, §5)."""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, ClassVar

import pytest
from pydantic import Field

import docuconf
from docuconf import (
    BinaryFile,
    ConfigValidationError,
    DeclarationError,
    DocuconfSettings,
    Duration,
    Keys,
    KeySet,
    Meta,
)

OLD, NEW = "o" * 32, "n" * 32


class Webhooks(DocuconfSettings):
    docuconf_service: ClassVar[str] = "webhooks"

    webhook_keys: Annotated[KeySet, Keys(key_min_length=32, key_max_length=256)] = Field(
        description="Keys that verify webhook signatures"
    )
    api_keys: Annotated[KeySet, Keys(max_keys=3, encoding="json")] | None = Field(
        None, description="Keys that callers present"
    )
    signing_keys: Annotated[KeySet, Keys(encoding="indexed")] | None = Field(
        None, description="Keys that sign session cookies"
    )


def load(cls: Any, **env: str) -> Any:
    return cls.load(env=env)


def codes(err: ConfigValidationError) -> list[tuple[str, str]]:
    return [(v.input, v.code) for v in err.violations]


# -- keySet -------------------------------------------------------------------


def test_key_set_contract() -> None:
    data = docuconf.contract_data(Webhooks)["vars"]
    assert data["WEBHOOK_KEYS"] == {
        "type": "keySet",
        "description": "Keys that verify webhook signatures",
        "required": True,
        "secret": True,
        "encoding": "csv",
        "separator": ",",
        "minKeys": 1,
        "maxKeys": 2,
        "keyMinLength": 32,
        "keyMaxLength": 256,
    }
    assert data["API_KEYS"]["encoding"] == "json" and "separator" not in data["API_KEYS"]
    assert data["API_KEYS"]["maxKeys"] == 3
    assert data["SIGNING_KEYS"]["encoding"] == "indexed"


def test_key_set_value() -> None:
    s = load(Webhooks, WEBHOOK_KEYS=f"{OLD},{NEW}", API_KEYS='["a","b,c"]', SIGNING_KEYS__0="x", SIGNING_KEYS__1="y")
    assert [k.get_secret_value() for k in s.webhook_keys.keys] == [OLD, NEW]
    assert len(s.webhook_keys) == 2
    assert [k.get_secret_value() for k in s.api_keys] == ["a", "b,c"]
    assert [k.get_secret_value() for k in s.signing_keys.keys] == ["x", "y"]
    assert s.webhook_keys.contains(NEW) and s.webhook_keys.contains(OLD.encode())
    assert not s.webhook_keys.contains(NEW[:-1]) and not s.webhook_keys.contains("")


def test_key_set_verify_tries_every_key() -> None:
    keys = KeySet([OLD, NEW])
    body = b"payload"
    tried: list[bytes] = []

    def check(key: bytes) -> bool:
        tried.append(key)
        return hmac.compare_digest(hmac.new(key, body, hashlib.sha256).digest(), signature)

    signature = hmac.new(OLD.encode(), body, hashlib.sha256).digest()
    assert keys.verify(check)
    assert tried == [OLD.encode(), NEW.encode()]  # no early exit after the first match
    signature = b"bad"
    assert not keys.verify(check)


def test_key_set_is_never_printed() -> None:
    s = load(Webhooks, WEBHOOK_KEYS=f"{OLD},{NEW}")
    for text in (repr(s), str(s), repr(s.webhook_keys), str(s.webhook_keys), f"{s.webhook_keys}"):
        assert OLD not in text and NEW not in text
    assert s.model_dump(mode="json")["webhook_keys"] == "**********"
    assert "**********" in repr(s.webhook_keys)


def test_key_set_logs_redacted(caplog: pytest.LogCaptureFixture) -> None:
    s = load(Webhooks, WEBHOOK_KEYS=OLD)
    with caplog.at_level(logging.INFO):
        logging.getLogger("app").info("keys: %s %r", s.webhook_keys, s.webhook_keys)
    assert OLD not in caplog.text


@pytest.mark.parametrize(
    ("env", "code"),
    [
        ({"WEBHOOK_KEYS": f"{OLD},"}, "out_of_range"),  # an empty key, from a stray separator
        ({"WEBHOOK_KEYS": f"{OLD},{NEW[:10]}"}, "out_of_range"),
        ({"WEBHOOK_KEYS": "k" * 257}, "out_of_range"),
        ({"WEBHOOK_KEYS": f"{OLD},{NEW},{OLD}"}, "too_many_items"),
        ({"WEBHOOK_KEYS": OLD, "API_KEYS": "[]"}, "too_few_items"),
        ({"WEBHOOK_KEYS": OLD, "API_KEYS": '["a",""]'}, "out_of_range"),
        ({"WEBHOOK_KEYS": OLD, "API_KEYS": '{"a": "b"}'}, "invalid_type"),
        ({"WEBHOOK_KEYS": OLD, "API_KEYS": "[1, 2]"}, "invalid_type"),
        ({"WEBHOOK_KEYS": OLD, "SIGNING_KEYS__0": "x", "SIGNING_KEYS__2": "y"}, "invalid_type"),
        ({"WEBHOOK_KEYS": "vault:secret/data/hooks#keys-0123456789abcdef"}, "invalid_type"),
        ({}, "missing_required"),
    ],
)
def test_key_set_errors_never_print_a_key(env: dict[str, str], code: str) -> None:
    with pytest.raises(ConfigValidationError) as info:
        load(Webhooks, **env)
    assert [c for _, c in codes(info.value)] == [code]
    text = str(info.value)
    for value in env.values():
        for key in [value, *value.split(",")]:
            if len(key) >= 4:
                assert key not in text, text


def test_key_set_keys_are_never_trimmed() -> None:
    s = load(Webhooks, WEBHOOK_KEYS=f" {OLD} ")
    assert s.webhook_keys.contains(f" {OLD} ")


def test_key_set_empty_is_unset() -> None:
    s = load(Webhooks, WEBHOOK_KEYS=OLD, API_KEYS="")
    assert s.api_keys is None


def test_key_set_declaration_errors() -> None:
    class Bad(DocuconfSettings):
        a: Annotated[KeySet, Keys(min_keys=0)] = Field(description="Bad minimum")
        b: Annotated[KeySet, Keys(min_keys=3, max_keys=2)] = Field(description="Bad maximum")
        c: Annotated[KeySet, Keys(key_min_length=10, key_max_length=5)] = Field(description="Bad lengths")
        d: Annotated[str, Keys()] = Field(description="Not a key set")
        e: KeySet | None = Field(None, description="Examples of a secret", examples=["abc"])
        f: KeySet = Field(description="A Field bound", max_length=3)

    with pytest.raises(DeclarationError) as info:
        docuconf.declaration(Bad)
    text = str(info.value)
    assert "min_keys must be at least 1" in text
    assert "max_keys must be at least min_keys" in text
    assert "key_min_length 10 is above key_max_length 5" in text
    assert "Keys() applies to KeySet fields" in text
    assert "a secret must not have examples" in text
    assert "not Field(max_length=...)" in text


def test_key_set_defaults_without_keys() -> None:
    class S(DocuconfSettings):
        keys: KeySet = Field(description="Keys with the default bounds")

    assert docuconf.contract_data(S, name="svc")["vars"]["KEYS"]["maxKeys"] == 2
    with pytest.raises(ConfigValidationError) as info:
        S.load(env={"KEYS": "a,b,c"})
    assert codes(info.value) == [("KEYS", "too_many_items")]


# -- deprecated -----------------------------------------------------------------


class Ports(DocuconfSettings):
    docuconf_service: ClassVar[str] = "ports"

    port: int = Field(8080, ge=1, description="Port to listen on")
    old_port: Annotated[int | None, Meta(replaced_by="PORT")] = Field(
        None, ge=1, deprecated="Use PORT instead", description="Old name of the listen port"
    )
    old_token: str | None = Field(None, deprecated="The billing API no longer takes a token", description="Old token")
    geoip: Annotated[Path, BinaryFile(path="/data/geoip/geoip.mmdb"), Meta(replaced_by="geo-db")] | None = Field(
        None, deprecated="Use geo-db instead", description="GeoIP database"
    )


def test_deprecated_contract() -> None:
    data = docuconf.contract_data(Ports)
    assert data["vars"]["OLD_PORT"]["deprecated"] == {"message": "Use PORT instead", "replacedBy": "PORT"}
    assert data["vars"]["OLD_TOKEN"]["deprecated"] == {"message": "The billing API no longer takes a token"}
    assert data["files"]["geoip"]["deprecated"] == {"message": "Use geo-db instead", "replacedBy": "geo-db"}


def test_deprecated_warns_at_boot_without_the_value(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    (tmp_path / "data/geoip").mkdir(parents=True)
    (tmp_path / "data/geoip/geoip.mmdb").write_bytes(b"db")
    with caplog.at_level(logging.WARNING, logger="docuconf"):
        s = Ports.load(env={"OLD_PORT": "9090", "OLD_TOKEN": "tok-0123456789", "DOCUCONF_FILE_ROOT": str(tmp_path)})
    assert s.__dict__["old_port"] == 9090  # still loaded
    text = caplog.text
    assert "OLD_PORT" in text and "Use PORT instead" in text
    assert "OLD_TOKEN" in text and "no longer takes a token" in text
    assert "geoip" in text and "Use geo-db instead" in text
    assert "9090" not in text and "tok-0123456789" not in text


def test_deprecated_is_quiet_when_unset(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="docuconf"):
        Ports.load(env={})
    assert "deprecated" not in caplog.text


def test_deprecated_is_still_checked() -> None:
    with pytest.raises(ConfigValidationError) as info:
        Ports.load(env={"OLD_PORT": "0"})
    assert codes(info.value) == [("OLD_PORT", "out_of_range")]


def test_deprecated_rules() -> None:
    class Bad(DocuconfSettings):
        a: int = Field(description="Required and deprecated", deprecated="Use B instead")
        b: int | None = Field(None, description="Blank message", deprecated="   ")
        c: int | None = Field(None, description="Too long a message", deprecated="x" * 501)
        d: int | None = Field(None, description="No message at all", deprecated=True)
        e: Annotated[int | None, Meta(replaced_by="A")] = Field(None, description="Replaced, not deprecated")

    with pytest.raises(DeclarationError) as info:
        docuconf.declaration(Bad)
    text = str(info.value)
    assert "A (a): a required variable cannot be deprecated" in text
    assert text.count("deprecated must say what to use instead") == 2
    assert "at most 500 characters" in text
    assert 'E (e): replaced_by needs Field(deprecated="...")' in text

    class Ok(DocuconfSettings):
        a: int | None = Field(None, description="At the limit", deprecated="x" * 500)

    docuconf.declaration(Ok)


# -- strict parsing ---------------------------------------------------------------


class Strict(DocuconfSettings):
    docuconf_service: ClassVar[str] = "strict"

    flag: bool | None = Field(None, description="A switch")
    count: int | None = Field(None, description="A count")
    ratio: float | None = Field(None, description="A ratio")
    wait: timedelta | None = Field(None, description="A wait, in ISO 8601")
    wait_go: Annotated[timedelta, Duration("go")] | None = Field(None, description="A wait, in Go syntax")
    ids: docuconf.CsvList[int] | None = Field(None, description="Some ids")
    names: docuconf.CsvList[str] | None = Field(None, description="Some names")


@pytest.mark.parametrize(
    ("name", "raw", "want"),
    [
        ("FLAG", "TRUE", True),
        ("FLAG", "False", False),
        ("COUNT", "+5", 5),
        ("COUNT", "007", 7),
        ("COUNT", "-0", 0),
        ("RATIO", "1E3", 1000.0),
        ("RATIO", "007.5", 7.5),
        ("WAIT", "PT1,5S", timedelta(seconds=1.5)),
        ("WAIT", "P1DT2H", timedelta(hours=26)),
        ("WAIT_GO", "-1m30s", timedelta(seconds=-90)),
        ("WAIT_GO", "1.5h", timedelta(minutes=90)),
        ("IDS", "+1,007,-0", [1, 7, 0]),
        ("NAMES", "a, b ,c", ["a", " b ", "c"]),
        ("NAMES", "a,,b", ["a", "", "b"]),
    ],
)
def test_strict_accepts(name: str, raw: str, want: Any) -> None:
    s = Strict.load(env={name: raw})
    assert getattr(s, name.lower()) == want


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        *[("FLAG", x) for x in ("1", "0", "t", "f", "yes", "no", "on", "off", " true", "false ", "true\n")],
        *[("COUNT", x) for x in ("0x10", "0o17", "0b101", "1_000", "1e3", "5.0", " 5", "5\n", "-", "+-5", "١٢")],
        *[("RATIO", x) for x in ("0x1p4", "inf", "Infinity", "nan", ".5", "5.", "1_000.5", " 1.5", "1e", "1e400")],
        *[("WAIT", x) for x in ("pt90s", "PT", "P1W", "P1M", "-PT5S", "90", "1:30:00")],
        *[("WAIT_GO", x) for x in ("5", "5S", "1d", "1m 30s", "5s\n")],
        *[("IDS", x) for x in ("1, 2", "1,,2", "1,0x10")],
    ],
)
def test_strict_rejects(name: str, raw: str) -> None:
    with pytest.raises(ConfigValidationError) as info:
        Strict.load(env={name: raw})
    assert codes(info.value) == [(name, "invalid_type")]
