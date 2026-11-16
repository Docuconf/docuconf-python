"""Conditional requirements (SPEC §4.2.1), roles (SPEC §4.9) and fragments (SPEC §4.8) in the contract-first mode."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import docuconf
from docuconf import ConfigValidationError, DeclarationError
from docuconf.contract import RequiredIf


def _contract(vars_: dict[str, Any], **rest: Any) -> dict[str, Any]:
    return {
        "apiVersion": "docuconf.dev/v1alpha1",
        "kind": "ConfigContract",
        "metadata": {"name": "relay"},
        "vars": vars_,
        **rest,
    }


RELAY = _contract(
    {
        "TLS_ENABLED": {"type": "bool", "description": "Serve HTTPS", "default": False},
        "LOG_FORMAT": {
            "type": "enum",
            "description": "Log output format",
            "values": ["text", "json"],
            "default": "text",
        },
        "LOG_SCHEMA_URL": {"type": "url", "description": "Schema of the logs", "requiredIf": {"LOG_FORMAT": "json"}},
        "CA_SUBJECT": {"type": "string", "description": "Client CA subject", "requiredIf": {"TLS_ENABLED": True}},
        "WORKERS": {"type": "int", "description": "Worker threads", "default": 1},
        "POOL_NAME": {"type": "string", "description": "Name of the worker pool", "requiredIf": {"WORKERS": 1}},
        "OTEL_SERVICE_NAME": {"type": "string", "description": "Service name reported in telemetry"},
    },
    includes=["otel@1"],
    files={
        "allowlist": {
            "type": "text",
            "description": "Allowed clients",
            "path": "/etc/relay/allow.txt",
            "requiredIf": {"TLS_ENABLED": True},
        }
    },
    profiles={"selector": "LOG_FORMAT", "default": "text", "defaults": {"json": {"WORKERS": 4}}},
)

SHOP = _contract(
    {
        "DOCUCONF_ROLE": {
            "type": "enum",
            "description": "The role this workload runs",
            "required": True,
            "values": ["web", "worker"],
        },
        "ORIGINS": {
            "type": "list",
            "items": "string",
            "description": "Allowed origins",
            "required": True,
            "roles": ["web"],
        },
        "QUEUES": {
            "type": "list",
            "items": "string",
            "description": "Job queues",
            "required": True,
            "roles": ["worker"],
        },
        "BATCH": {
            "type": "int",
            "description": "Jobs per batch",
            "roles": ["worker"],
            "requiredIf": {"DOCUCONF_ROLE": "worker"},
        },
    },
    roles=["web", "worker"],
    files={
        "robots": {
            "type": "text",
            "description": "The robots.txt served",
            "path": "/etc/shop/robots.txt",
            "required": True,
            "roles": ["web"],
        }
    },
)


def _problems(contract: dict[str, Any], env: dict[str, str], root: Path) -> list[str]:
    try:
        docuconf.load_contract(contract, {**env, "DOCUCONF_FILE_ROOT": str(root)}, termination_log=False)
    except ConfigValidationError as e:
        return sorted(str(v).replace(str(root), "") for v in e.violations)
    return []


def _codes(contract: dict[str, Any], env: dict[str, str], root: Path) -> list[str]:
    try:
        docuconf.load_contract(contract, {**env, "DOCUCONF_FILE_ROOT": str(root)}, termination_log=False)
    except ConfigValidationError as e:
        return sorted(f"{v.input}:{v.code}" for v in e.violations)
    return []


def test_reads_the_conditions_roles_and_includes() -> None:
    relay = docuconf.contract_settings(RELAY)
    assert relay.docuconf_includes == ("otel@1",)
    assert relay.docuconf_roles == ()
    assert relay.docuconf_required_if["CA_SUBJECT"] == RequiredIf("TLS_ENABLED", True)
    assert str(relay.docuconf_required_if["LOG_SCHEMA_URL"]) == 'LOG_FORMAT is "json"'
    assert str(relay.docuconf_required_if["POOL_NAME"]) == "WORKERS is 1"
    assert relay.docuconf_required_if["allowlist"].var == "TLS_ENABLED"

    shop = docuconf.contract_settings(SHOP, role="worker")
    assert shop.docuconf_roles == ("web", "worker")
    assert shop.docuconf_role == "worker"
    assert shop.docuconf_input_roles == {
        "BATCH": ("worker",),
        "ORIGINS": ("web",),
        "QUEUES": ("worker",),
        "robots": ("web",),
    }
    assert docuconf.contract_settings(SHOP, role="cron").docuconf_role is None


def test_a_condition_compares_bool_and_int_apart() -> None:
    assert RequiredIf("A", True).holds_for(True)
    assert not RequiredIf("A", True).holds_for(1)
    assert not RequiredIf("A", 1).holds_for(True)
    assert RequiredIf("A", 1).holds_for(1)
    assert not RequiredIf("A", "1").holds_for(1)


def test_a_condition_that_holds_makes_its_inputs_required(tmp_path: Path) -> None:
    # WORKERS defaults to 1, so POOL_NAME is required by the default.
    assert _problems(RELAY, {}, tmp_path) == ["POOL_NAME [missing_required]: is required when WORKERS is 1"]
    assert _problems(RELAY, {"WORKERS": "2"}, tmp_path) == []
    assert _problems(RELAY, {"TLS_ENABLED": "true", "LOG_FORMAT": "json"}, tmp_path) == [
        "CA_SUBJECT [missing_required]: is required when TLS_ENABLED is true",
        'LOG_SCHEMA_URL [missing_required]: is required when LOG_FORMAT is "json"',
        "allowlist [file_missing]: not found at /etc/relay/allow.txt (put it under DOCUCONF_FILE_ROOT=), "
        "and is required when TLS_ENABLED is true",
    ]


def test_a_condition_on_a_profile_default(tmp_path: Path) -> None:
    # The json profile sets WORKERS to 4: POOL_NAME is not required; the environment beats the profile.
    assert _codes(RELAY, {"LOG_FORMAT": "json", "LOG_SCHEMA_URL": "https://x.example/s.json"}, tmp_path) == []
    env = {"LOG_FORMAT": "json", "LOG_SCHEMA_URL": "https://x.example/s.json", "WORKERS": "1"}
    assert _codes(RELAY, env, tmp_path) == ["POOL_NAME:missing_required"]


def test_conditional_inputs_set(tmp_path: Path) -> None:
    (tmp_path / "etc/relay").mkdir(parents=True)
    (tmp_path / "etc/relay/allow.txt").write_text("billing\n")
    env = {"TLS_ENABLED": "TRUE", "CA_SUBJECT": "CN=CA", "POOL_NAME": "main", "DOCUCONF_FILE_ROOT": str(tmp_path)}
    values = docuconf.load_contract(RELAY, env, termination_log=False)
    assert values.value("CA_SUBJECT") == "CN=CA"
    assert values.value("allowlist") == "billing\n"


def test_an_invalid_condition_variable_makes_nothing_required(tmp_path: Path) -> None:
    assert _codes(RELAY, {"TLS_ENABLED": "yes", "WORKERS": "2"}, tmp_path) == ["TLS_ENABLED:invalid_type"]
    assert _codes(RELAY, {"WORKERS": "one"}, tmp_path) == ["WORKERS:invalid_type"]


def test_a_role_requires_only_its_own_inputs(tmp_path: Path) -> None:
    assert _codes(SHOP, {"DOCUCONF_ROLE": "web"}, tmp_path) == ["ORIGINS:missing_required", "robots:file_missing"]
    assert _codes(SHOP, {"DOCUCONF_ROLE": "worker"}, tmp_path) == [
        "BATCH:missing_required",
        "QUEUES:missing_required",
    ]
    assert _codes(SHOP, {"DOCUCONF_ROLE": "worker", "QUEUES": "mail", "BATCH": "10"}, tmp_path) == []
    # With no role, or an unknown one, the role is the one problem reported.
    assert _codes(SHOP, {}, tmp_path) == ["DOCUCONF_ROLE:missing_required"]
    assert _codes(SHOP, {"DOCUCONF_ROLE": "cron"}, tmp_path) == ["DOCUCONF_ROLE:not_in_enum"]


def test_a_condition_of_another_role_requires_nothing(tmp_path: Path) -> None:
    contract = _contract(
        {**SHOP["vars"], "BATCH": {**SHOP["vars"]["BATCH"], "requiredIf": {"DOCUCONF_ROLE": "web"}}},
        roles=["web", "worker"],
    )
    # BATCH is a worker input: a condition that holds in the web role does not make it required there.
    assert _codes(contract, {"DOCUCONF_ROLE": "web", "ORIGINS": "https://shop.example"}, tmp_path) == []


_B_BOOL = {"type": "bool", "description": "A flag"}
_A = {"type": "string", "description": "An input"}


@pytest.mark.parametrize(
    ("vars_", "problem"),
    [
        ({"A": {**_A, "requiredIf": {"A": "x"}}}, "A: requiredIf must name another declared variable, not 'A'"),
        ({"A": {**_A, "requiredIf": {"B": "x"}}}, "A: requiredIf must name another declared variable, not 'B'"),
        (
            {"A": {**_A, "required": True, "requiredIf": {"B": True}}, "B": _B_BOOL},
            "A: an input with requiredIf cannot be required or have a default",
        ),
        (
            {"A": {**_A, "default": "x", "requiredIf": {"B": True}}, "B": _B_BOOL},
            "A: an input with requiredIf cannot be required or have a default",
        ),
        (
            {"A": {**_A, "deprecated": {"message": "Going away"}, "requiredIf": {"B": True}}, "B": _B_BOOL},
            "A: a conditionally required input cannot be deprecated: the platform could not stop setting it",
        ),
        ({"A": {**_A, "requiredIf": {"B": 1}}, "B": _B_BOOL}, "A: requiredIf: B is 1 is not a value B can have"),
        (
            {"A": {**_A, "requiredIf": {"B": True}}, "B": {"type": "int", "description": "A count"}},
            "A: requiredIf: B is true is not a value B can have",
        ),
        (
            {"A": {**_A, "requiredIf": {"B": "x"}}, "B": {**_A, "secret": True}},
            "A: requiredIf names B, which is not a plain string, int, bool or enum variable",
        ),
        (
            {"A": {**_A, "requiredIf": {"B": "1s"}}, "B": {"type": "duration", "description": "A timeout"}},
            "A: requiredIf names B, which is not a plain string, int, bool or enum variable",
        ),
        (
            {"A": {**_A, "requiredIf": {"B": "x", "C": "y"}}},
            "A: requiredIf must name one variable and its value: {VAR: value}",
        ),
        (
            {"A": {**_A, "requiredIf": {"B": 1.5}}, "B": {"type": "int", "description": "A count"}},
            "A: requiredIf must name one variable and a string, integer or boolean value",
        ),
        ({"A": {**_A, "roles": ["web"]}}, "A: has roles, but the contract declares none"),
        ({"A": {**_A, "roles": []}}, "A: roles must be a list of at least one role name"),
    ],
)
def test_rejects_a_bad_condition_or_role(vars_: dict[str, Any], problem: str) -> None:
    with pytest.raises(DeclarationError) as info:
        docuconf.contract_settings(_contract(vars_))
    assert problem in info.value.problems


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ({"type": "enum", "description": "A mode", "values": ["a", "b"]}, "c"),
        ({"type": "int", "description": "A count", "min": 1, "max": 9}, 10),
        ({"type": "string", "description": "A name", "maxLength": 2}, "abc"),
    ],
)
def test_rejects_a_value_the_condition_variable_cannot_hold(var: dict[str, Any], value: Any) -> None:
    with pytest.raises(DeclarationError) as info:
        docuconf.contract_settings(_contract({"A": {**_A, "requiredIf": {"B": value}}, "B": var}))
    assert any(p.startswith("A: requiredIf: B is ") and "is not a value B can have" in p for p in info.value.problems)


def test_a_condition_on_a_file_is_checked() -> None:
    contract = _contract(
        {}, files={"allow": {"type": "text", "description": "Allowed clients", "path": "/a", "requiredIf": {"B": 1}}}
    )
    with pytest.raises(DeclarationError) as info:
        docuconf.contract_settings(contract)
    assert "file allow: requiredIf must name another declared variable, not 'B'" in info.value.problems


def test_roles_need_the_role_variable() -> None:
    with pytest.raises(DeclarationError) as info:
        docuconf.contract_settings(_contract({"A": {**_A, "roles": ["cron"]}}, roles=["web", "web", "Web"]))
    problems = info.value.problems
    assert "role web is listed twice" in problems
    assert "role 'Web' must be a DNS label" in problems
    assert "A: role cron is not one of the contract's roles, web, web, Web" in problems
    assert any(p.startswith("DOCUCONF_ROLE: a contract with roles declares it as a required enum") for p in problems)


@pytest.mark.parametrize(
    "role_var",
    [
        {"type": "enum", "description": "The role", "values": ["web", "worker"]},  # not required
        {"type": "enum", "description": "The role", "required": True, "values": ["worker", "web"]},  # other order
        {"type": "string", "description": "The role", "required": True},
        {"type": "enum", "description": "The role", "required": True, "values": ["web", "worker"], "roles": ["web"]},
    ],
)
def test_rejects_a_role_variable_that_does_not_name_the_roles(role_var: dict[str, Any]) -> None:
    with pytest.raises(DeclarationError) as info:
        docuconf.contract_settings(_contract({"DOCUCONF_ROLE": role_var}, roles=["web", "worker"]))
    assert any(p.startswith("DOCUCONF_ROLE: a contract with roles declares it") for p in info.value.problems)


@pytest.mark.parametrize(
    ("includes", "problem"),
    [
        ("otel@1", "includes must be a list of fragment names, such as otel@1"),
        (["otel"], "includes: 'otel' is not a fragment name, name@major"),
        (["otel@1", "otel@1"], "includes: otel@1 is listed twice"),
    ],
)
def test_rejects_bad_includes(includes: Any, problem: str) -> None:
    with pytest.raises(DeclarationError) as info:
        docuconf.contract_settings(_contract({}, includes=includes))
    assert problem in info.value.problems
