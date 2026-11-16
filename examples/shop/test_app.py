"""The shop matrix: roles, requiredIf and the otel@1 fragment, checked at boot (``python -m pytest``)."""

from __future__ import annotations

from pathlib import Path

import pytest
from app import describe, load, main

from docuconf import ConfigValidationError

BASE = {"DATABASE_URL": "postgres://shop@db:5432/shop"}
WEB = {"DOCUCONF_ROLE": "web", "ALLOWED_ORIGINS": "https://shop.example"}
WORKER_JSON_LOGS = {"DOCUCONF_ROLE": "worker", "QUEUES": "default", "LOG_FORMAT": "json"}
WORKER_WEBHOOKS = {"DOCUCONF_ROLE": "worker", "QUEUES": "default", "WEBHOOKS_ENABLED": "true"}
KEY = "k" * 40

# (env on top of BASE, whether to write the signing key under the file root, the line printed at startup)
OK = {
    "1-web": (WEB, False, "shop web: port 8080, origins https://shop.example"),
    "2-worker-needs-no-origins": (
        {"DOCUCONF_ROLE": "worker", "QUEUES": "default,mail"},
        False,
        "shop worker: queues default,mail, webhooks off",
    ),
    "7-json-logs-with-schema": (
        {**WORKER_JSON_LOGS, "LOG_SCHEMA_URL": "https://logs.example/v1.json"},
        False,
        "shop worker: queues default, webhooks off",
    ),
    "9-webhooks-with-key": (WORKER_WEBHOOKS, True, "shop worker: queues default, webhooks on"),
    "10-key-belongs-to-worker": (
        {**WEB, "WEBHOOKS_ENABLED": "true"},
        False,
        "shop web: port 8080, origins https://shop.example",
    ),
}

# (env on top of BASE, the only violation as (input, code), a part of its message or None)
BAD = {
    "3-web-needs-origins": ({"DOCUCONF_ROLE": "web"}, ("ALLOWED_ORIGINS", "missing_required"), None),
    "4-no-role": ({"QUEUES": "default"}, ("DOCUCONF_ROLE", "missing_required"), None),
    "5-unknown-role": ({"DOCUCONF_ROLE": "cron"}, ("DOCUCONF_ROLE", "not_in_enum"), None),
    "6-json-logs-need-schema": (
        WORKER_JSON_LOGS,
        ("LOG_SCHEMA_URL", "missing_required"),
        'is required when LOG_FORMAT is "json"',
    ),
    "8-webhooks-need-key": (
        WORKER_WEBHOOKS,
        ("webhook-signing-key", "file_missing"),
        ", and is required when WEBHOOKS_ENABLED is true",
    ),
    "11-fragment-var-checked": (
        {**WEB, "OTEL_EXPORTER_OTLP_PROTOCOL": "carrier-pigeon"},
        ("OTEL_EXPORTER_OTLP_PROTOCOL", "not_in_enum"),
        None,
    ),
}


def env_for(case: dict[str, str], root: Path) -> dict[str, str]:
    """The case's env over the base, with ``root`` (a fresh, empty directory) as the file root."""
    return {**BASE, **case, "DOCUCONF_FILE_ROOT": str(root)}


@pytest.mark.parametrize(("case", "with_key", "line"), OK.values(), ids=OK.keys())
def test_valid_config_starts(case: dict[str, str], with_key: bool, line: str, tmp_path: Path) -> None:
    if with_key:
        key = tmp_path / "etc/shop/webhooks/signing.key"
        key.parent.mkdir(parents=True)
        key.write_text(KEY)
    config = load(env_for(case, tmp_path))
    assert describe(config) == line


def test_queues_come_typed(tmp_path: Path) -> None:
    # The printed queues are the typed list, not the raw value.
    config = load(env_for({"DOCUCONF_ROLE": "worker", "QUEUES": "default,mail"}, tmp_path))
    assert config.value("QUEUES") == ["default", "mail"]


@pytest.mark.parametrize(("case", "violation", "message"), BAD.values(), ids=BAD.keys())
def test_invalid_config_fails_at_boot(
    case: dict[str, str], violation: tuple[str, str], message: str | None, tmp_path: Path
) -> None:
    with pytest.raises(ConfigValidationError) as info:
        load(env_for(case, tmp_path))
    assert [(v.input, v.code) for v in info.value.violations] == [violation]
    if message is not None:
        assert message in info.value.violations[0].message


def test_main_lists_every_problem_exits_1_and_hides_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    secret = "postgres-password-1234"
    monkeypatch.setenv("DATABASE_URL", f"mysql://shop:{secret}@db:3306/shop")  # the wrong scheme
    monkeypatch.setenv("DOCUCONF_ROLE", "web")
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    monkeypatch.setenv("DOCUCONF_FILE_ROOT", str(tmp_path))
    monkeypatch.setenv("DOCUCONF_TERMINATION_LOG", str(tmp_path / "termination-log"))
    with pytest.raises(SystemExit) as info:
        main()
    assert info.value.code == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("docuconf: 2 configuration problems:\n")
    assert "ALLOWED_ORIGINS [missing_required]" in err and "DATABASE_URL [" in err
    assert secret not in err and "Traceback" not in err
    assert (tmp_path / "termination-log").read_text().strip() == err.strip()
