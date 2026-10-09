"""The webhook key set: a rotation, step by step, and the key sets that fail at boot (``python -m pytest``)."""

from __future__ import annotations

import hashlib
import hmac

import pytest
from app import Settings, public_config, verify
from pydantic import SecretStr

from docuconf import ConfigValidationError

OLD, NEW = "o" * 32, "n" * 32
BODY = b'{"order":"42","status":"paid"}'
BASE = {"DATABASE_URL": "postgres://orders:pw@db:5432/orders"}


def sign(key: str) -> str:
    return hmac.new(key.encode(), BODY, hashlib.sha256).hexdigest()


def keys(value: str) -> list[SecretStr] | None:
    """WEBHOOK_KEYS as the service loads it at boot."""
    return Settings.load(env={**BASE, "WEBHOOK_KEYS": value}, termination_log=False).webhook_keys


@pytest.mark.parametrize(
    ("value", "accepts"),
    [
        (OLD, {OLD: True, NEW: False}),  # before
        (f"{OLD},{NEW}", {OLD: True, NEW: True}),  # the overlap
        (NEW, {OLD: False, NEW: True}),  # after
    ],
    ids=["before", "overlap", "after"],
)
def test_rotation(value: str, accepts: dict[str, bool]) -> None:
    ks = keys(value)
    for key, want in accepts.items():
        assert verify(ks, BODY, sign(key)) is want


def test_bad_signatures() -> None:
    ks = keys(OLD)
    assert not verify(ks, BODY, "")
    assert not verify(ks, BODY, "not hex")
    assert not verify(ks, BODY, sign("x" * 32))
    assert not verify(ks, BODY + b" ", sign(OLD))
    assert not verify(None, BODY, sign(OLD))


def test_config_redacts_the_keys() -> None:
    settings = Settings.load(env={**BASE, "WEBHOOK_KEYS": f"{OLD},{NEW}"}, termination_log=False)
    assert public_config(settings)["WEBHOOK_KEYS"] == "***"
    assert OLD not in repr(settings)
    assert public_config(Settings.load(env=BASE, termination_log=False))["WEBHOOK_KEYS"] == "***"


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (f"{OLD},", "out_of_range"),  # an empty second key
        (f"{OLD},{NEW[:10]}", "out_of_range"),  # a truncated key
        (f"{OLD},{NEW},{'x' * 32}", "too_many_items"),
    ],
)
def test_bad_key_sets_fail_at_boot_without_printing_a_key(value: str, code: str) -> None:
    with pytest.raises(ConfigValidationError) as info:
        keys(value)
    assert [(v.input, v.code) for v in info.value.violations] == [("WEBHOOK_KEYS", code)]
    assert OLD not in str(info.value) and NEW[:10] not in str(info.value)
