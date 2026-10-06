"""Boot validation of environment variables (SPEC §5, §6, §11.2 item 5)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import pytest
from pydantic import AliasChoices, BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

import docuconf
from docuconf import ConfigValidationError, Csv, Secret, Url
from tests.fixtures.sample_settings import GatewaySettings, LogLevel


def load_error(cls: type[BaseSettings] = GatewaySettings) -> ConfigValidationError:
    with pytest.raises(ConfigValidationError) as info:
        docuconf.load(cls, watch=False)
    return info.value


def codes(err: ConfigValidationError) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for v in err.violations:
        out.setdefault(v.input, []).append(v.code)
    return out


def test_valid_environment(gateway_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "9090")
    monkeypatch.setenv("DEBUG", "TRUE")
    monkeypatch.setenv("LOG_LEVEL", "warn")
    monkeypatch.setenv("REQUEST_TIMEOUT", "PT90S")
    monkeypatch.setenv("WORKER_PORTS", "[8081,8082]")
    monkeypatch.setenv("RATE_LIMITS", '{"perMinute":60}')
    monkeypatch.setenv("SAMPLE_RATE", "0.5")
    s = docuconf.load(GatewaySettings, watch=False)
    assert s.port == 9090
    assert s.debug is True
    assert s.log_level is LogLevel.warn
    assert s.request_timeout.total_seconds() == 90
    assert s.worker_ports == [8081, 8082]
    assert s.allowed_origins == ["https://a.example.com", "https://b.example.com"]
    assert s.rate_limits is not None and s.rate_limits.perMinute == 60
    assert s.sample_rate == 0.5
    assert s.database_url.get_secret_value().startswith("postgres://")


def test_bad_int(gateway_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "80a")
    err = load_error()
    assert codes(err) == {"PORT": ["invalid_type"]}
    assert "80a" in str(err)


def test_out_of_range_int(gateway_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "70000")
    assert codes(load_error()) == {"PORT": ["out_of_range"]}


def test_int_outside_64_bits(gateway_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOMEMLIMIT", str(2**63))
    assert codes(load_error()) == {"GOMEMLIMIT": ["out_of_range"]}


@pytest.mark.parametrize("value", ["nan", "inf", "-Infinity"])
def test_float_must_be_finite(gateway_root: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SAMPLE_RATE", value)
    assert codes(load_error()) == {"SAMPLE_RATE": ["invalid_type"]}


def test_missing_required(gateway_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REGION")
    err = load_error()
    assert codes(err) == {"REGION": ["missing_required"]}


def test_empty_means_unset_for_non_strings(gateway_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "")
    monkeypatch.setenv("DEBUG", "")
    monkeypatch.setenv("GOMEMLIMIT", "")
    monkeypatch.setenv("REQUEST_TIMEOUT", "")
    monkeypatch.setenv("LOG_LEVEL", "")
    s = docuconf.load(GatewaySettings, watch=False)
    assert (s.port, s.debug, s.gomemlimit, s.log_level) == (8080, False, None, LogLevel.info)


def test_empty_required_non_string_is_missing(gateway_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOWED_ORIGINS", "")
    monkeypatch.setenv("PUBLIC_URL", "")
    assert codes(load_error()) == {"ALLOWED_ORIGINS": ["missing_required"], "PUBLIC_URL": ["missing_required"]}


def test_empty_string_is_a_value_for_strings(gateway_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGION", "")
    # Present, so it fails minLength rather than being "missing".
    assert codes(load_error()) == {"REGION": ["out_of_range"]}


def test_values_are_not_trimmed(gateway_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGION", "eu-west-1\n")
    assert codes(load_error()) == {"REGION": ["pattern_mismatch"]}


def test_enum_and_scheme_and_list_bounds(gateway_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "verbose")
    monkeypatch.setenv("MODE", "primary")
    monkeypatch.setenv("PUBLIC_URL", "http://gateway.example.com")
    monkeypatch.setenv("ALLOWED_ORIGINS", ",".join(f"https://{i}.example.com" for i in range(11)))
    assert codes(load_error()) == {
        "ALLOWED_ORIGINS": ["too_many_items"],
        "LOG_LEVEL": ["not_in_enum"],
        "MODE": ["not_in_enum"],
        "PUBLIC_URL": ["invalid_scheme"],
    }


def test_list_item_bounds(gateway_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_PORTS", "[8081,0]")
    assert codes(load_error()) == {"WORKER_PORTS": ["out_of_range"]}
    monkeypatch.setenv("WORKER_PORTS", "[65536]")
    assert codes(load_error()) == {"WORKER_PORTS": ["out_of_range"]}
    monkeypatch.setenv("WORKER_PORTS", "[1,65535]")
    assert docuconf.load(GatewaySettings, watch=False).worker_ports == [1, 65535]


def test_malformed_json_is_reported_with_the_rest(gateway_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # pydantic-settings raises on JSON it cannot decode; docuconf reports it and still checks the others.
    monkeypatch.setenv("WORKER_PORTS", "[8081,")
    monkeypatch.setenv("RATE_LIMITS", "{perMinute: 60}")
    monkeypatch.setenv("PORT", "0")
    assert codes(load_error()) == {
        "PORT": ["out_of_range"],
        "RATE_LIMITS": ["invalid_type"],
        "WORKER_PORTS": ["invalid_type"],
    }
    assert os.environ["WORKER_PORTS"] == "[8081,"


@pytest.mark.parametrize("value", ['["8081"]', "[8081.0]", "[true]"])
def test_json_int_list_items_must_be_json_integers(
    gateway_root: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("WORKER_PORTS", value)
    assert codes(load_error()) == {"WORKER_PORTS": ["invalid_type"]}


def test_list_items_outside_64_bits(gateway_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class S(BaseSettings):
        ids: Annotated[list[int], NoDecode, Csv()] = Field(default_factory=list, description="Record ids")

    monkeypatch.setenv("IDS", f"1,{2**63}")
    assert codes(load_error(S)) == {"IDS": ["out_of_range"]}


def test_url_needs_scheme(gateway_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLIC_URL", "gateway.example.com")
    assert codes(load_error()) == {"PUBLIC_URL": ["invalid_type"]}


def test_duration_iso8601(gateway_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUEST_TIMEOUT", "PT10M")
    assert codes(load_error()) == {"REQUEST_TIMEOUT": ["out_of_range"]}
    monkeypatch.setenv("REQUEST_TIMEOUT", "1m30s")  # Go syntax is not this host's encoding
    assert codes(load_error()) == {"REQUEST_TIMEOUT": ["invalid_type"]}


def test_json_var_schema_mismatch(gateway_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMITS", '{"perMinute": 0, "extra": 1}')
    err = load_error()
    assert codes(err) == {"RATE_LIMITS": ["schema_mismatch", "schema_mismatch"]}
    monkeypatch.setenv("RATE_LIMITS", "{not json")
    assert codes(load_error()) == {"RATE_LIMITS": ["invalid_type"]}


def test_secret_values_never_printed(gateway_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "mysql://app:hunter2-very-secret@db/app")
    monkeypatch.setenv("KEYSTORE_PASSWORD", "")
    err = load_error()
    assert codes(err)["DATABASE_URL"] == ["invalid_scheme"]
    text = str(err) + repr(err.violations)
    assert "hunter2" not in text
    log = Path(str(gateway_root.parent / "termination-log")).read_text()
    assert "hunter2" not in log


@pytest.mark.parametrize(
    ("value", "scheme"),
    [
        ("vault:secret/data/gateway/db#url-hunter2", "vault:"),
        ("op://prod/gateway/db-hunter2", "op://"),
        ("ref+awssm://gateway/db-hunter2", "ref+"),
    ],
)
def test_unresolved_injector_reference(
    gateway_root: Path, monkeypatch: pytest.MonkeyPatch, value: str, scheme: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", value)
    err = load_error()
    assert codes(err) == {"DATABASE_URL": ["invalid_type"]}
    assert str(err.violations[0]) == (
        f"DATABASE_URL [invalid_type]: holds an unresolved {scheme} reference; "
        "the injector that should resolve it did not run"
    )
    text = str(err) + repr(err.violations)
    log = Path(str(gateway_root.parent / "termination-log")).read_text()
    for out in (text, log):
        assert "hunter2" not in out
        assert value not in out
    assert "DATABASE_URL" in log


def test_injector_reference_only_flagged_for_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-secret variable is checked by its own constraints only.
    class S(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="APP_")
        note: str = Field(description="Free-form note")
        token: SecretStr = Field(description="Resolved API token")

    monkeypatch.setenv("APP_NOTE", "vault:not-a-reference")
    monkeypatch.setenv("APP_TOKEN", "s3cr3t-vault:op://")
    s = docuconf.load(S, watch=False)
    assert s.note == "vault:not-a-reference"
    assert s.token.get_secret_value() == "s3cr3t-vault:op://"


def test_secret_custom_validator_message_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    class S(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="APP_")
        token: SecretStr = Field(description="API token for the upstream")

        @field_validator("token")
        @classmethod
        def check(cls, v: SecretStr) -> SecretStr:
            raise ValueError(f"bad token {v.get_secret_value()}")

    monkeypatch.setenv("APP_TOKEN", "tok-123456")
    err = load_error(S)
    assert codes(err) == {"APP_TOKEN": ["invalid_type"]}
    assert "tok-123456" not in str(err)


def test_all_violations_together(gateway_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "zero")
    monkeypatch.delenv("REGION")
    monkeypatch.setenv("LOG_LEVEL", "loud")
    (gateway_root / "etc/gateway/license/license.key").write_text("not a licence")
    (gateway_root / "etc/gateway/routes/routes.yaml").unlink()
    err = load_error()
    assert codes(err) == {
        "LOG_LEVEL": ["not_in_enum"],
        "PORT": ["invalid_type"],
        "REGION": ["missing_required"],
        "license": ["pattern_mismatch"],
        "routes": ["file_missing"],
    }
    assert str(err).startswith("docuconf: 5 configuration problems:")


def test_termination_log(gateway_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "custom-log"
    monkeypatch.setenv("DOCUCONF_TERMINATION_LOG", str(target))
    monkeypatch.delenv("REGION")
    load_error()
    assert "REGION [missing_required]" in target.read_text()


def test_case_insensitive_names(gateway_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("port", "7000")
    assert docuconf.load(GatewaySettings, watch=False).port == 7000


def test_prefix_alias_and_nested(monkeypatch: pytest.MonkeyPatch) -> None:
    class Db(BaseModel):
        host: str = Field(description="Database host name")
        port: int = Field(5432, description="Database port", ge=1)

    class S(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="APP_", env_nested_delimiter="__")
        name: str = Field(description="Service display name", validation_alias=AliasChoices("SVC_NAME", "NAME"))
        db: Db = Field(description="Database settings")
        hosts: Annotated[list[str], NoDecode, Csv(";")] = Field(default_factory=list, description="Peer hosts")
        api_key: Annotated[str, Secret()] = Field(description="Key for the partner API")
        callback: Annotated[str | None, Url(schemes=("https",))] = Field(None, description="Callback endpoint")

    contract = docuconf.contract_data(S, name="svc")
    assert sorted(contract["vars"]) == [
        "APP_API_KEY",
        "APP_CALLBACK",
        "APP_DB__HOST",
        "APP_DB__PORT",
        "APP_HOSTS",
        "SVC_NAME",
    ]
    assert contract["vars"]["APP_HOSTS"]["separator"] == ";"
    assert contract["vars"]["APP_API_KEY"]["secret"] is True
    assert contract["vars"]["APP_DB__PORT"]["default"] == 5432

    monkeypatch.setenv("SVC_NAME", "x")
    monkeypatch.setenv("APP_DB__HOST", "db")
    monkeypatch.setenv("APP_DB__PORT", "")
    monkeypatch.setenv("APP_HOSTS", "a;b")
    monkeypatch.setenv("APP_API_KEY", "k")
    s = docuconf.load(S, watch=False)
    assert (s.name, s.db.host, s.db.port, s.hosts) == ("x", "db", 5432, ["a", "b"])

    monkeypatch.setenv("APP_DB__PORT", "0")
    monkeypatch.setenv("APP_CALLBACK", "ftp://x")
    assert codes(load_error(S)) == {"APP_CALLBACK": ["invalid_scheme"], "APP_DB__PORT": ["out_of_range"]}


def test_model_validator_errors_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    class S(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="APP_")
        low: int = Field(1, description="Lower bound")
        high: int = Field(2, description="Upper bound")

        @model_validator(mode="after")
        def ordered(self) -> S:
            if self.low > self.high:
                raise ValueError("low must not exceed high")
            return self

    monkeypatch.setenv("APP_LOW", "5")
    err = load_error(S)
    assert err.violations[0].kind == "model"
    assert "low must not exceed high" in err.violations[0].message


def test_dotenv_is_opt_in_and_env_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("APP_PORT=\nAPP_NAME=from-dotenv\n")

    class S(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="APP_", env_file=str(env_file))
        port: int = Field(8080, description="HTTP listen port")
        name: str = Field(description="Service display name")

    s = docuconf.load(S, watch=False)
    assert (s.port, s.name) == (8080, "from-dotenv")
    monkeypatch.setenv("APP_NAME", "from-env")
    assert docuconf.load(S, watch=False).name == "from-env"


def test_mixin(monkeypatch: pytest.MonkeyPatch) -> None:
    class S(docuconf.DocuconfSettings, BaseSettings):
        model_config = SettingsConfigDict(env_prefix="APP_")
        port: int = Field(8080, description="HTTP listen port")

    monkeypatch.setenv("APP_PORT", "1234")
    assert S.load().port == 1234
