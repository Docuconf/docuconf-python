"""Regression tests for the first-use review: secrets, testing, Settings() vs load(), the CLI, error messages."""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import threading
import warnings
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

import pytest
from pydantic import AliasPath, BaseModel, Field, PostgresDsn, Secret, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

import docuconf
from docuconf import (
    ConfigFile,
    ConfigValidationError,
    Csv,
    CsvList,
    DeclarationError,
    DocuconfSettings,
    DocuconfWarning,
    Duration,
    IndexedList,
    TextFile,
    Url,
)
from tests.certs import make_leaf, write_tls

ROOT = Path(__file__).parent.parent
PW = "hunter2-pw"
TOKEN = "sk_hunter4"


def violations(cls: type[BaseSettings], env: dict[str, str], **kw: Any) -> list[tuple[str, str]]:
    with pytest.raises(ConfigValidationError) as info:
        docuconf.load(cls, env=env, **kw)
    return [(v.input, v.code) for v in info.value.violations]


def problems(cls: type[BaseSettings]) -> str:
    with pytest.raises(DeclarationError) as info:
        docuconf.contract_data(cls, name="svc")
    return str(info.value)


# -- secrets never print -------------------------------------------------------


class Leaky(DocuconfSettings):
    database_url: Annotated[PostgresDsn, docuconf.Secret()] = Field(description="Postgres connection string")
    api_token: SecretStr = Field(description="Payments API token")
    pin: Secret[int] = Field(description="Numeric PIN for the vault")
    region: str = Field("eu", description="Deploy region")
    note: str = Field("", description="Free text note")

    @field_validator("note")
    @classmethod
    def no_secret_in_note(cls, v: str) -> str:
        if v == "quote":
            raise ValueError(f"note quotes the password {PW} and {TOKEN}")
        return v

    @model_validator(mode="after")
    def eu_only(self) -> Leaky:
        if self.region == "us":
            raise ValueError(
                f"region us needs an eu database, got {self.database_url} / {self.api_token.get_secret_value()}"
            )
        return self


LEAKY_ENV = {"DATABASE_URL": f"postgres://u:{PW}@h/d", "API_TOKEN": TOKEN, "PIN": "4711"}


def test_secret_marker_is_redacted_in_repr_and_str() -> None:
    s = Leaky.load(env=LEAKY_ENV)
    for text in (repr(s), str(s)):
        assert PW not in text and TOKEN not in text and "4711" not in text
        assert "database_url='**********'" in text
    assert s.pin.get_secret_value() == 4711
    assert docuconf.contract_data(Leaky, name="svc")["vars"]["PIN"] == {
        "type": "int",
        "description": "Numeric PIN for the vault",
        "required": True,
        "secret": True,
    }


def test_secret_text_file_is_redacted_in_repr(tmp_path: Path) -> None:
    class S(DocuconfSettings):
        license_key: Annotated[str, docuconf.Secret(), TextFile(path="/etc/app/license.key")] = Field(
            description="Licence key for the product"
        )

    (tmp_path / "etc/app").mkdir(parents=True)
    (tmp_path / "etc/app/license.key").write_text("LICENSE-SECRET-123")
    s = S.load(env={}, file_root=str(tmp_path))
    assert "LICENSE-SECRET-123" not in repr(s)


def test_contract_first_values_redact_secrets() -> None:
    contract = {
        "apiVersion": "docuconf.dev/v1alpha1",
        "kind": "ConfigContract",
        "metadata": {"name": "svc"},
        "vars": {"DB_PASSWORD": {"type": "string", "description": "Database password", "secret": True}},
    }
    values = docuconf.load_contract(contract, {"DB_PASSWORD": PW}, termination_log=False)
    assert PW not in repr(values) and PW not in str(values)


def test_model_validator_message_is_scrubbed(tmp_path: Path) -> None:
    log = tmp_path / "termination-log"
    with pytest.raises(ConfigValidationError) as info:
        Leaky.load(env={**LEAKY_ENV, "REGION": "us"}, termination_log=str(log))
    for text in (str(info.value), log.read_text()):
        assert PW not in text and TOKEN not in text
    assert "region us needs an eu database" in str(info.value)


def test_non_secret_field_validator_quoting_a_secret_is_scrubbed() -> None:
    with pytest.raises(ConfigValidationError) as info:
        Leaky.load(env={**LEAKY_ENV, "NOTE": "quote"})
    assert PW not in str(info.value) and TOKEN not in str(info.value)


def test_short_secrets_are_scrubbed_as_whole_words() -> None:
    from docuconf.errors import Violation
    from docuconf.loader import scrub_secrets

    [v] = scrub_secrets([Violation("X", "model", "invalid_type", "pin 42 in 1427")], ["42"])
    assert v.message == "pin ********** in 1427"


def test_secret_marker_inside_a_nested_model_is_a_declaration_error() -> None:
    class Db(BaseModel):
        password: Annotated[str, docuconf.Secret()] = Field(description="Database password")

    class S(DocuconfSettings):
        model_config = SettingsConfigDict(env_nested_delimiter="__")
        db: Db = Field(description="Database settings")

    assert "DB__PASSWORD (db.password): Secret() inside the nested model Db" in problems(S)


def test_typo_hint_never_shows_the_value() -> None:
    class S(DocuconfSettings):
        database_url: SecretStr = Field(description="Database connection string")

    with pytest.warns(DocuconfWarning, match="DATABSE_URL is set but not declared; did you mean DATABASE_URL") as rec:
        S.load(env={"DATABASE_URL": "postgres://h/d", "DATABSE_URL": PW})
    assert all(PW not in str(w.message) for w in rec)


# -- testing: env= ----------------------------------------------------------------


def test_env_mapping_never_reads_or_changes_os_environ(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, str | None] = {}

    class S(DocuconfSettings):
        port: int = Field(8080, description="HTTP listen port")
        hosts: Annotated[list[str], IndexedList()] = Field(default_factory=list, description="Upstream hosts")
        tags: list[str] = Field(default_factory=list, description="Some tags here")

        @model_validator(mode="after")
        def snapshot(self) -> S:
            seen.update(HOSTS=os.environ.get("HOSTS"), TAGS=os.environ.get("TAGS"))
            return self

    monkeypatch.setenv("PORT", "1")
    monkeypatch.setenv("HOSTS", "x")
    monkeypatch.setenv("TAGS", "[bad")
    monkeypatch.setenv("DOCUCONF_TERMINATION_LOG", str(tmp_path / "log"))
    before = dict(os.environ)
    assert S.load(env={"HOSTS__0": "a"}).model_dump() == {"port": 8080, "hosts": ["a"], "tags": []}
    assert violations(S, {"TAGS": "[bad"}) == [("TAGS", "invalid_type")]
    assert dict(os.environ) == before
    assert seen == {"HOSTS": "x", "TAGS": "[bad"}  # never deleted, even while loading
    assert not (tmp_path / "log").exists()


def test_process_environment_is_not_mutated_while_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str | None] = {}

    class S(DocuconfSettings):
        hosts: Annotated[list[str], IndexedList()] = Field(default_factory=list, description="Upstream hosts")
        tags: list[str] = Field(default_factory=list, description="Some tags here")

        @model_validator(mode="after")
        def snapshot(self) -> S:
            seen.update(HOSTS=os.environ.get("HOSTS"), TAGS=os.environ.get("TAGS"))
            return self

    monkeypatch.setenv("HOSTS", "x")
    monkeypatch.setenv("HOSTS__0", "a")
    assert S().hosts == ["a"]
    assert seen["HOSTS"] == "x"
    monkeypatch.setenv("TAGS", "[bad")
    with pytest.raises(ConfigValidationError):
        S()
    assert os.environ["TAGS"] == "[bad" and os.environ["HOSTS"] == "x"


def test_env_mapping_with_file_root(tmp_path: Path) -> None:
    class Rates(BaseModel):
        per_minute: int = Field(ge=1)

    class S(DocuconfSettings):
        rates: Annotated[Rates, ConfigFile(path="/etc/app/rates.json")] = Field(description="Rate limits")

    (tmp_path / "etc/app").mkdir(parents=True)
    (tmp_path / "etc/app/rates.json").write_text('{"per_minute": 5}')
    assert S.load(env={}, file_root=str(tmp_path)).rates.per_minute == 5
    assert S.load(env={"DOCUCONF_FILE_ROOT": str(tmp_path)}).rates.per_minute == 5


def test_env_values_must_be_strings() -> None:
    class S(DocuconfSettings):
        port: int = Field(8080, description="HTTP listen port")

    with pytest.raises(TypeError, match="env= must map names to strings"):
        S.load(env={"PORT": 8080})  # type: ignore[dict-item]


@pytest.fixture
def tls_root(tmp_path: Path) -> Path:
    write_tls(tmp_path / "etc/app/tls", make_leaf(["app.internal"]))
    return tmp_path


class Watched(DocuconfSettings):
    tls: Annotated[docuconf.TlsKeyPair, docuconf.TlsFile(path="/etc/app/tls", reload="watch")] = Field(
        description="Serving certificate"
    )


def _watch_threads() -> int:
    return sum(t.name == "docuconf-watch" and t.is_alive() for t in threading.enumerate())


def test_watcher_threads_stop_with_their_settings(tls_root: Path) -> None:
    before = _watch_threads()
    assert Watched.load(env={}, file_root=str(tls_root)) is not None
    assert _watch_threads() == before  # env= starts no watcher
    loaded = [Watched.load(env={}, file_root=str(tls_root), watch=True, watch_interval=0.01) for _ in range(5)]
    assert _watch_threads() == before + 5
    assert docuconf.get_watcher(loaded[0]) is not None
    del loaded
    gc.collect()
    for _ in range(200):
        if _watch_threads() == before:
            break
        threading.Event().wait(0.01)
    assert _watch_threads() == before


# -- Settings() is load() ------------------------------------------------------------


def test_settings_constructor_runs_docuconf(tls_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class S(DocuconfSettings):
        port: int = Field(8080, ge=1, description="HTTP listen port")
        tls: Annotated[docuconf.TlsKeyPair, docuconf.TlsFile(path="/etc/app/tls")] = Field(
            description="Serving certificate"
        )

    monkeypatch.setenv("DOCUCONF_FILE_ROOT", str(tls_root))
    monkeypatch.setenv("DOCUCONF_TERMINATION_LOG", str(tls_root / "log"))
    monkeypatch.setenv("PORT", "")  # empty means unset (SPEC §5), not int_parsing
    s = S()
    assert s.port == 8080 and isinstance(s.tls, docuconf.TlsKeyPair)
    assert s.model_dump(exclude={"tls"}) == docuconf.load(S).model_dump(exclude={"tls"})
    monkeypatch.setenv("PORT", "0")
    with pytest.raises(ConfigValidationError) as info:
        S()
    assert info.value.codes == ["out_of_range"]
    assert S(port=9).port == 9  # constructor arguments still win


def test_plain_base_settings_is_a_declaration_error() -> None:
    class Plain(BaseSettings):
        port: int = Field(8080, description="HTTP listen port")

    assert "Plain: subclass docuconf.DocuconfSettings instead of BaseSettings" in problems(Plain)
    with pytest.raises(DeclarationError):
        docuconf.load(Plain, env={})


def test_wrong_base_order_says_how_to_fix_it() -> None:
    with pytest.raises(TypeError, match="put DocuconfSettings before BaseSettings"):

        class S(BaseSettings, DocuconfSettings):  # type: ignore[misc]
            pass


def test_load_an_instance_says_pass_the_class() -> None:
    class S(DocuconfSettings):
        port: int = Field(8080, description="HTTP listen port")

    with pytest.raises(TypeError, match="takes the settings class, not an instance"):
        docuconf.load(S.load(env={}), env={})  # type: ignore[type-var]


def test_load_or_exit_prints_once_and_exits_1(tmp_path: Path) -> None:
    script = tmp_path / "boot.py"
    script.write_text(
        "from pydantic import Field, SecretStr\n"
        "from docuconf import DocuconfSettings\n"
        "class Settings(DocuconfSettings):\n"
        "    port: int = Field(8080, ge=1, description='HTTP listen port')\n"
        "    api_token: SecretStr = Field(description='Token for the payments API')\n"
        "Settings.load_or_exit()\n"
    )
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src"), "PORT": "0"}
    env["DOCUCONF_TERMINATION_LOG"] = str(tmp_path / "log")
    r = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True)
    assert r.returncode == 1
    assert r.stderr == (
        "docuconf: 2 configuration problems:\n"
        "  - API_TOKEN [missing_required]: required, but not set\n"
        "  - PORT [out_of_range]: Input should be greater than or equal to 1 (got '0')\n"
    )
    assert (tmp_path / "log").read_text() == r.stderr.rstrip("\n")


# -- declarations fail loudly --------------------------------------------------------


def test_markers_on_types_they_do_not_fit() -> None:
    class U(DocuconfSettings):
        port: Annotated[int, Url()] = Field(8080, description="HTTP listen port")

    assert "Url() applies to str, SecretStr or AnyUrl fields, not int" in problems(U)

    class C(DocuconfSettings):
        name: Annotated[str, NoDecode, Csv()] = Field("x", description="Service display name")

    assert "Csv() applies to list fields, not str" in problems(C)

    class D(DocuconfSettings):
        timeout: Annotated[int, Duration("go")] = Field(30, description="Timeout in seconds")

    assert "Duration() applies to timedelta fields, not int" in problems(D)


def test_alias_path_and_extra_allow_are_declaration_errors() -> None:
    class A(DocuconfSettings):
        port: int = Field(8080, description="HTTP listen port", validation_alias=AliasPath("server", "port"))

    assert "AliasPath(...) is not supported" in problems(A)

    class E(DocuconfSettings):
        model_config = SettingsConfigDict(extra="allow")
        port: int = Field(8080, description="HTTP listen port")

    assert 'extra="allow" loads environment variables the contract does not list' in problems(E)


def test_enum_default_and_bounds_are_checked() -> None:
    from typing import Literal

    class S(DocuconfSettings):
        level: Literal["a", "b"] = Field("c", description="Some level here")  # type: ignore[assignment]
        port: int = Field(0, ge=1, description="HTTP listen port")

    p = problems(S)
    assert "LEVEL (level): default 'c' does not satisfy" in p
    assert "PORT (port): default 0 does not satisfy" in p


def test_csv_list() -> None:
    class S(DocuconfSettings):
        origins: CsvList[str] = Field(default_factory=list, description="CORS origins allowed")
        ports: CsvList[int] = Field(default_factory=list, description="Ports to listen on")

    s = S.load(env={"ORIGINS": "a,b", "PORTS": "1,2"})
    assert s.origins == ["a", "b"] and s.ports == [1, 2]
    v = docuconf.contract_data(S, name="svc")["vars"]
    assert v["ORIGINS"]["encoding"] == "csv" and v["PORTS"]["items"] == "int"


def test_file_input_env_alias() -> None:
    assert ConfigFile(path="/etc/a.json", env="A_FILE").path_env == "A_FILE"
    with pytest.raises(TypeError):
        ConfigFile(path="/etc/a.json", env="A_FILE", path_env="B_FILE")


def test_declaration_warnings_are_python_warnings() -> None:
    class S(DocuconfSettings):
        ff_new_checkout: bool = Field(False, description="Use the new checkout")

    with pytest.warns(DocuconfWarning, match="FF_NEW_CHECKOUT: looks like a feature flag"):
        S.load(env={})


def test_typo_hints() -> None:
    class S(DocuconfSettings):
        model_config = SettingsConfigDict(env_prefix="APP_")
        port: int = Field(8080, description="HTTP listen port")
        log_level: str = Field("info", description="Minimum log level")

    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        S.load(env={"APP_PROT": "1", "APP_LOG_LEVL": "x", "APP_HOST": "h", "PROT": "1", "APP_PORT__0": "1"})
    msgs = sorted(str(w.message) for w in rec if issubclass(w.category, DocuconfWarning))
    assert msgs == [
        "docuconf: APP_LOG_LEVL is set but not declared; did you mean APP_LOG_LEVEL?",
        "docuconf: APP_PROT is set but not declared; did you mean APP_PORT?",
    ]


# -- error messages --------------------------------------------------------------------


def test_duration_messages() -> None:
    class S(DocuconfSettings):
        timeout: timedelta = Field(timedelta(seconds=30), le=timedelta(minutes=5), description="Upstream timeout")
        go: Annotated[timedelta, Duration("go")] = Field("1m", description="A Go style duration")

    with pytest.raises(ConfigValidationError) as info:
        S.load(env={"TIMEOUT": "30s", "GO": "PT1M"})
    text = str(info.value)
    assert "TIMEOUT [invalid_type]: expected an ISO 8601 duration like PT30S (got '30s'); to accept values" in text
    assert "GO [invalid_type]: expected a Go duration like 30s (got 'PT1M')" in text
    with pytest.raises(ConfigValidationError) as info:
        S.load(env={"TIMEOUT": "PT10M"})
    assert "(got 'PT10M')" in str(info.value)
    assert S.load(env={}).go == timedelta(minutes=1)
    assert docuconf.contract_data(S, name="svc")["vars"]["GO"]["default"] == "1m"


def test_file_missing_says_how_to_supply_it() -> None:
    class S(DocuconfSettings):
        rates: Annotated[dict[str, int], ConfigFile(path="/etc/app/rates.json", path_env="RATES_FILE")] = Field(
            description="Rate limits per client"
        )

    with pytest.raises(ConfigValidationError) as info:
        S.load(env={})
    assert "(set RATES_FILE to its path, or set DOCUCONF_FILE_ROOT to a directory holding etc/app/rates.json" in str(
        info.value
    )


def test_secret_string_length_is_out_of_range() -> None:
    class S(DocuconfSettings):
        token: SecretStr = Field(min_length=20, description="Token for the payments API")

    with pytest.raises(ConfigValidationError) as info:
        S.load(env={"TOKEN": "short"})
    assert [str(v) for v in info.value.violations] == [
        "TOKEN [out_of_range]: should have at least 20 characters, has 5"
    ]


# -- cryptography is optional ------------------------------------------------------------------

NO_CRYPTO = """
import sys
sys.modules["cryptography"] = None
from typing import Annotated
from pydantic import Field
import docuconf

class Env(docuconf.DocuconfSettings):
    port: int = Field(8080, description="HTTP listen port")

print(Env.load(env={"PORT": "9"}).port)

class Tls(docuconf.DocuconfSettings):
    tls: Annotated[docuconf.TlsKeyPair, docuconf.TlsFile(path="/etc/app/tls")] = Field(description="Serving cert")

try:
    Tls.load(env={})
except docuconf.DeclarationError as e:
    print(e)
"""


def test_cryptography_is_only_needed_for_tls_inputs() -> None:
    r = subprocess.run(
        [sys.executable, "-c", NO_CRYPTO],
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("9\n")
    assert "TlsFile needs the cryptography package: pip install 'docuconf-pydantic[tls]'" in r.stdout
