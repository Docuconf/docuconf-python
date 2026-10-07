# docuconf-pydantic

Typed configuration contracts for [pydantic-settings](https://github.com/pydantic/pydantic-settings).

[docuconf](https://github.com/docuconf/docuconf-go/blob/main/spec/SPEC.md) treats an application's configuration
(environment variables, config files, TLS certificates, CA bundles, keystores) as an API between the app and the
Kubernetes platform that runs it. With this package a Python service keeps its pydantic-settings class, and gets:

- **one check at boot** that reports *every* problem at once, each with a stable error code, and never prints a
  secret;
- **a CUE contract** (`contract.cue`) exported from the class, which the platform validates its values against
  before deploying.

Descriptions come from `Field(description=...)`, constraints from `ge`/`le`/`min_length`/`max_length`/`pattern`,
enums from `Literal` or `Enum`, secrets from `SecretStr`. docuconf adds only what pydantic cannot express: URL
schemes, CSV lists, file inputs and a secret marker for other types.

**Example service:** [`examples/orders/`](examples/orders/), a small `http.server` app with its exported contract
and a smoke test.

## 1. Install

The package is not on PyPI yet. Install it from GitHub:

```sh
pip install "docuconf-pydantic @ git+https://github.com/docuconf/docuconf-python@main"
pip install "docuconf-pydantic[tls,yaml] @ git+https://github.com/docuconf/docuconf-python@main"
```

Python 3.10+. Extras: `tls` (the `cryptography` package) for TLS, CA bundle and keystore inputs; `yaml` (PyYAML)
for YAML config files; `jsonschema` for the contract-first mode. In a `requirements.txt`, use the same
`docuconf-pydantic @ git+...` line; with uv, `uv add "docuconf-pydantic @ git+https://github.com/docuconf/docuconf-python"`.
`pip install docuconf-pydantic` will work once the first release is on PyPI. The distribution is
`docuconf-pydantic`; the import package is `docuconf`.

## 2. Declare

Subclass `docuconf.DocuconfSettings`, which is pydantic-settings' `BaseSettings` with docuconf's checks in its
constructor:

```python
# orders/settings.py
from datetime import timedelta
from typing import Annotated, ClassVar, Literal

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from docuconf import CsvList, DocuconfSettings, Url


class Settings(DocuconfSettings):
    docuconf_service: ClassVar[str] = "orders"  # metadata.name in the contract
    model_config = SettingsConfigDict(env_prefix="ORDERS_")

    port: int = Field(8080, ge=1, le=65535, description="HTTP listen port")
    log_level: Literal["debug", "info", "warn", "error"] = Field("info", description="Minimum log level")
    database_url: Annotated[SecretStr, Url(schemes=("postgres", "postgresql"))] = Field(
        description="Primary Postgres connection string"
    )
    timeout: timedelta = Field(timedelta(seconds=30), le=timedelta(minutes=5), description="Upstream timeout")
    allowed_origins: CsvList[str] = Field(default_factory=list, description="CORS origins allowed to call the API")
```

Every variable needs a description of at least 5 characters. Mistakes in the declaration (a default outside its
bounds, `Url()` on an `int`, a secret with a default...) raise `docuconf.DeclarationError` naming the variable and
the fix, at the latest when the class is first loaded or exported.

### Descriptions and details

The contract's `description` is a one-line summary; `details` is optional CommonMark (at most 4000 characters) for
why the input exists and when to change it. Write the details as the field's attribute docstring, the string on the
line after it:

```python
class Settings(DocuconfSettings):
    timeout: timedelta = Field(timedelta(seconds=30), description="Upstream timeout")
    """Raise it for batch clients.

    Keep it below the load balancer's idle timeout, or clients see a reset rather than a ``504``.
    """

    region: str = "eu-west-1"
    """Cloud region for object storage.

    Change it together with the bucket: ``eu-west-1`` or ``us-east-1``.
    """

    workers: int = Field(4, description="Worker processes", json_schema_extra={"details": "One per core."})
```

- With `Field(description=...)`, the whole docstring is the details.
- Without one, the docstring's first paragraph is the description (on one line, without a final period) and the
  rest is the details, as with `use_attribute_docstrings=True`.
- `Field(json_schema_extra={"details": "..."})` sets the details explicitly, for a class whose source is not
  available.
- reStructuredText in docstrings becomes CommonMark: double-backquoted literals and Sphinx roles (`:class:`,
  `:meth:`...) become code spans, `::` and `.. code-block::` blocks become fenced code, and field lists
  (`:param x:`) are dropped.

Export fails when an input has no description, or details that are blank or longer than 4000 characters. Details
are for docs only and never read at runtime. `docuconf docs` in the [docuconf CLI](https://github.com/docuconf/docuconf-go)
generates `CONFIG.md` and `CONFIG.agents.md` from the exported contract.

## 3. Run

Load the settings at the entry point:

```python
# orders/main.py
from orders.settings import Settings

settings = Settings.load_or_exit()  # prints every problem and exits 1 if the configuration is wrong
print(f"orders listening on :{settings.port}, timeout {settings.timeout}")
```

```sh
ORDERS_DATABASE_URL=postgres://localhost/orders python -m orders.main
```

`Settings()` runs exactly the same checks as `docuconf.load(Settings)` and raises `docuconf.ConfigValidationError`
(never a raw pydantic `ValidationError`); `Settings.load_or_exit()` (or `docuconf.load_or_exit(Settings)`) turns
that into a clean exit. `repr(settings)` and `str(settings)` show secrets as `'**********'`.

## 4. See an error

```sh
ORDERS_PORT=70000 ORDERS_TIMEOUT=30s ORDERS_DATABASE_URL=mysql://localhost/orders python -m orders.main
```

The service exits with status 1 and lists every problem, with no traceback and no secret value:

```text
docuconf: 3 configuration problems:
  - ORDERS_DATABASE_URL [invalid_scheme]: URL scheme should be one of 'postgres', 'postgresql'
  - ORDERS_PORT [out_of_range]: Input should be less than or equal to 65535 (got '70000')
  - ORDERS_TIMEOUT [invalid_type]: expected an ISO 8601 duration like PT30S (got '30s'); to accept values like 30s, use Annotated[timedelta, docuconf.Duration("go")]
```

In Kubernetes the same text goes to `/dev/termination-log`, so `kubectl describe pod` shows it. A set variable
that is not declared but looks like a typo of one that is gets a warning (a `docuconf.DocuconfWarning`), never
showing its value: `docuconf: ORDERS_LOG_LEVL is set but not declared; did you mean ORDERS_LOG_LEVEL?`

## 5. Test your config

`load(env=...)` reads the given mapping instead of the process environment. It never reads or changes
`os.environ`, starts no watcher thread and writes no termination log, so tests need no `monkeypatch`:

```python
# tests/test_settings.py
import pytest

import docuconf
from orders.settings import Settings


def test_defaults() -> None:
    settings = Settings.load(env={"ORDERS_DATABASE_URL": "postgres://db/orders"})
    assert settings.port == 8080
    assert settings.allowed_origins == []


def test_every_problem_is_reported() -> None:
    with pytest.raises(docuconf.ConfigValidationError) as info:
        Settings.load(env={"ORDERS_PORT": "0"})
    assert info.value.codes == ["missing_required", "out_of_range"]
    assert [v.input for v in info.value.violations] == ["ORDERS_DATABASE_URL", "ORDERS_PORT"]
```

For file inputs, pass `file_root=` (a directory standing in for `/`, see [Local development](#local-development)).
`docuconf.load(Settings, env=...)` is the same as `Settings.load(env=...)`.

## 6. Export the contract

Export in CI, and commit or publish the contract with the image. Export needs no environment: while it imports
your module, a module-level `Settings()` or `load()` is not validated.

```sh
docuconf export orders.settings:Settings --out contract.cue
docuconf export orders.settings:Settings --out contract.cue --check
docuconf export orders.settings:Settings --name orders --app-version "$GIT_SHA"
```

`--check` writes nothing and fails when `contract.cue` is out of date. It ignores the generator version recorded
in the contract, so upgrading docuconf alone does not fail CI. From Python, `docuconf.to_contract(Settings)`
returns the CUE text and `docuconf.contract_data(Settings)` the same contract as plain data.

## 7. Deploy

The platform validates the values in its manifests against `contract.cue` before it deploys (see the
[spec](https://github.com/docuconf/docuconf-go/blob/main/spec/SPEC.md)). At boot, the app checks the real
environment and files again; a pod that fails exits 1 and shows the problems in `kubectl describe pod`.
`DOCUCONF_TERMINATION_LOG` changes the termination log path, and `load(..., termination_log=False)` turns it off.

## Framework integration

### FastAPI

`docuconf.fastapi.settings_dependency` (no extra dependency) loads the settings once, fails fast at startup and is
overridable in tests:

```python
# orders/api.py
from typing import Annotated

from fastapi import Depends, FastAPI

from docuconf.fastapi import settings_dependency
from orders.settings import Settings

get_settings = settings_dependency(Settings)
app = FastAPI(lifespan=get_settings.lifespan)  # loads and checks the settings before serving


@app.get("/port")
def port(settings: Annotated[Settings, Depends(get_settings)]) -> int:
    return settings.port
```

If the configuration is wrong, the lifespan prints the problems and exits the process with status 1, instead of
uvicorn's lifespan traceback and status 3 (`settings_dependency(Settings, exit_on_error=False)` raises instead).
With your own lifespan, call `get_settings.check(app)` in it. In tests, override the dependency; the lifespan uses
the override, so the process environment is never read:

```python
# tests/test_api.py
from fastapi.testclient import TestClient

from orders.api import app, get_settings
from orders.settings import Settings


def test_port() -> None:
    env = {"ORDERS_DATABASE_URL": "postgres://db/orders", "ORDERS_PORT": "9000"}
    app.dependency_overrides[get_settings] = lambda: Settings.load(env=env)
    with TestClient(app) as client:
        assert client.get("/port").json() == 9000
    app.dependency_overrides.clear()
```

### Django

Django reads its settings module at import time, so keep the class in a module of its own and load it in
`settings.py` with `load_or_exit`:

```python
# mysite/config.py
from typing import Annotated, ClassVar

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from docuconf import CsvList, DocuconfSettings, Url


class Env(DocuconfSettings):
    docuconf_service: ClassVar[str] = "mysite"
    model_config = SettingsConfigDict(env_prefix="DJANGO_")

    secret_key: SecretStr = Field(min_length=20, description="Django SECRET_KEY")
    debug: bool = Field(False, description="Django debug mode")
    allowed_hosts: CsvList[str] = Field(["localhost"], description="Host names the site serves")
    database_url: Annotated[SecretStr, Url(schemes=("postgres", "sqlite"))] = Field(description="Database URL")
```

```python
# mysite/settings.py (excerpt)
from mysite.config import Env

env = Env.load_or_exit()
SECRET_KEY = env.secret_key.get_secret_value()
DEBUG = env.debug
ALLOWED_HOSTS = env.allowed_hosts
```

A wrong environment makes every `manage.py` command and the WSGI/ASGI server print the problems and exit 1, with
no traceback. Export with `docuconf export mysite.config:Env --out contract.cue`. Commands that run at image build
time, such as `collectstatic`, import `settings.py` too, and docuconf never reads the build environment as
configuration (SPEC §11.2). Give those commands placeholder values, as you would with any settings library:

```sh
DJANGO_SECRET_KEY=build-only-not-a-secret-000 DJANGO_DATABASE_URL=sqlite:///build.db python manage.py collectstatic --noinput
```

[`tests/django_project/`](tests/django_project/) is this recipe as a Django project; CI runs `manage.py check`
and `collectstatic` against it.

## Local development

File inputs live at absolute paths such as `/etc/orders/tls`. `DOCUCONF_FILE_ROOT=dir` (or `load(...,
file_root=dir)`) prepends `dir` to every absolute file path, including paths read from a `path_env` variable. A
missing file says so:

```text
  - tls [file_missing]: TLS directory not found at /etc/orders/tls (set DOCUCONF_FILE_ROOT to a directory holding etc/orders/tls for local development)
```

For the [file inputs example](#file-inputs) below, create a development certificate whose name matches
`dns_names`, and the rates file:

```sh
mkdir -p dev/etc/orders/tls dev/etc/orders/rates
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 90 -subj /CN=orders.internal -addext subjectAltName=DNS:orders.internal -keyout dev/etc/orders/tls/tls.key -out dev/etc/orders/tls/tls.crt
echo '{"per_minute": 60}' > dev/etc/orders/rates/rates.json
```

Then run with `DOCUCONF_FILE_ROOT=dev`. A `.env` file works as in pydantic-settings when the class sets
`env_file=".env"` (real environment variables win).

## File inputs

A file input is a field annotated with a file marker. docuconf reads and checks the file, then passes the value to
the settings constructor, so the field has a typed value like any other. This is
[`examples/app/settings.py`](examples/app/settings.py), whose contract is
[`examples/app/contract.cue`](examples/app/contract.cue):

```python
# examples/app/settings.py
"""The README example: a service with env vars, a TLS key pair and a config file."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import NoDecode, SettingsConfigDict

from docuconf import ConfigFile, Csv, DocuconfSettings, TlsFile, TlsKeyPair, Url


class Rates(BaseModel):
    per_minute: int = Field(ge=1)
    burst: int = Field(0, ge=0)


class Settings(DocuconfSettings):
    docuconf_service: ClassVar[str] = "orders"
    model_config = SettingsConfigDict(env_prefix="ORDERS_")

    port: int = Field(8080, ge=1, le=65535, description="HTTP listen port")
    log_level: Literal["debug", "info", "warn", "error"] = Field("info", description="Minimum log level")
    database_url: Annotated[SecretStr, Url(schemes=("postgres", "postgresql"))] = Field(
        description="Primary Postgres connection string"
    )
    api_token: SecretStr = Field(min_length=20, description="Token for the payments API")
    timeout: timedelta = Field(timedelta(seconds=30), le=timedelta(minutes=5), description="Upstream timeout")
    allowed_origins: Annotated[list[str], NoDecode, Csv()] = Field(
        default_factory=list, description="CORS origins allowed to call the API"
    )

    tls: Annotated[
        TlsKeyPair,
        TlsFile(path="/etc/orders/tls", dns_names=("orders.internal",), min_remaining="720h", reload="watch"),
    ] = Field(description="Certificate the service serves HTTPS with")
    rates: Annotated[Rates, ConfigFile(path="/etc/orders/rates/rates.json", path_env="ORDERS_RATES_FILE")] = Field(
        description="Per-client rate limits"
    )
```

`settings.tls.server_context()` returns an `ssl.SSLContext` that reloads its certificate when the files rotate, and
`settings.rates.per_minute` is an `int`.

| marker | field type | checked at boot |
|---|---|---|
| `ConfigFile(path, format=json\|yaml\|toml)` | the model it binds to | parses (a UTF-8 byte-order mark is accepted) and validates against the model |
| `TlsFile(path, dns_names, key_algorithms, min_remaining, require_ca)` | `TlsKeyPair` | `tls.crt` and `tls.key` parse and match; validity and `min_remaining`; SAN DNS names (a wildcard covers one label); key algorithm; chain order; chain to `ca.crt` |
| `CaBundleFile(path, min_certificates)` | `CaBundle` | at least `min_certificates` parseable PEM certificates |
| `KeystoreFile(path, password_var)` | `Keystore` | the PKCS#12 file opens with the password variable |
| `TextFile(path, pattern, min_length, max_length)` | `str` (the content) or `Path` | UTF-8, length, pattern |
| `BinaryFile(path)` | `bytes` (the content) or `Path` | exists, readable, within `max_size` |

Every marker also takes `name` (by default the field name in kebab-case), `path_env` (or its alias `env`: a variable
the platform sets to the path), `reload` (`restart` or `watch`), `max_size`, `required` (by default, whether the
field is required) and `group`. An optional file input looks like `Annotated[CaBundle | None, CaBundleFile(...)] =
None`. TLS and keystore inputs are always secret; mark other secret files with `Secret()`. TLS, CA bundle and
keystore inputs need the `tls` extra; without it, the declaration error says so.

TLS checks use the `cryptography` package. The chain to `ca.crt` is verified with `cryptography.x509.verification`
(cryptography 45 or newer) under a permissive extension policy, since private CAs rarely follow the Web PKI profile;
CA certificates must still carry basicConstraints. With older cryptography, the SDK falls back to checking that the
top of `tls.crt` is, or is directly issued by, a certificate in `ca.crt`.

**`reload="watch"`**: `docuconf.load` starts a daemon thread that polls each watched input every 2 seconds
(`watch_interval=` changes this; `watch=False` turns it off). Kubernetes updates projected volumes by swapping a
symlink, so the watcher stats every file of the input through symlinks and re-reads all of them together when any
has changed. A reload that fails its checks is logged and the old value is kept. `TlsKeyPair`, `CaBundle` and
`Keystore` values are updated in place, and every `SSLContext` made by `TlsKeyPair.server_context()` or
`client_context()` loads the new certificate. Other values are replaced on the settings object. Listen with
`pair.on_change(callback)` or `docuconf.get_watcher(settings).on_reload(callback)`, and stop with
`docuconf.get_watcher(settings).stop()`. The watcher also stops when the settings object is garbage-collected.

## How the declaration maps to the contract

| pydantic-settings | contract |
|---|---|
| `str` (`min_length`, `max_length`, `pattern`) | `string` (`minLength`, `maxLength`, `pattern`) |
| `int` (`ge`, `le`; `gt`/`lt` become inclusive bounds ±1) | `int` (`min`, `max`) |
| `float` (`ge`, `le`) | `float` |
| `bool` | `bool` |
| `timedelta` (`ge`, `le`) | `duration`, `encoding: "iso8601"` |
| `Annotated[timedelta, Duration("go")]` (or `"seconds"`, `"timespan"`) | `duration` with that `encoding` |
| `AnyUrl`, `HttpUrl`, `PostgresDsn`..., or `Annotated[str \| SecretStr, Url(schemes=...)]` | `url` with `schemes` |
| `Annotated[str \| SecretStr, Url()]` with `Field(max_length=...)` | `url` with `maxLength` |
| `Literal["a", "b"]`, `Enum` of strings | `enum` |
| `list[str]`, `list[int]` (`min_length`, `max_length`) | `list` (`minItems`, `maxItems`), `encoding: "json"` |
| `list[Annotated[int, Field(ge=0, le=1023)]]`, `list[conint(ge=0)]` | `list` of `int` with `itemMin`, `itemMax` |
| `list[Annotated[str, Field(min_length=2, max_length=4)]]`, `list[constr(max_length=4)]` | `list` of `string` with `itemMinLength`, `itemMaxLength` |
| `CsvList[str]`, `CsvList[int]`, or `Annotated[list[str], NoDecode, Csv(";")]` | `list`, `encoding: "csv"`, `separator` |
| `Annotated[list[str], IndexedList()]` | `list`, `encoding: "indexed"` (`NAME__0`, `NAME__1`...) |
| a model, a `dict`, a list of models... | `json`, with `schema` from pydantic's JSON Schema |
| `Annotated[Model, JsonMaxLength(256)]` | `json` with `maxLength` |
| a nested model with `env_nested_delimiter` | one variable per field, e.g. `APP_DB__HOST` |
| `SecretStr`, `SecretBytes`, `pydantic.Secret[T]`, or `Annotated[T, Secret()]` | `secret: true` |
| `Field(examples=...)`, `Field(deprecated=...)` | `examples`, `deprecated` |
| `Annotated[T, Meta(group=..., replaced_by=..., config_key=...)]` | `group`, `deprecated.replacedBy`, `configKey` |
| `Annotated[T, Exclude()]` | left out (for values from a secrets manager, say) |

**Env names** follow pydantic-settings' own rules: `env_prefix` plus the field name, or the field's `alias` /
`validation_alias` (the first `AliasChoices` entry is exported; the others still work at boot). `AliasPath` and
`extra="allow"` are declaration errors, since the contract could not list what they read. Contracts use upper-case
names. pydantic-settings matches names case-insensitively by default, so `port` and `PORT` both work at boot. With
`case_sensitive=True`, the names in your class must already be upper-case.

**Secrets**: prefer `SecretStr` (or `pydantic.Secret[T]`), which keeps the value out of `repr()`, `model_dump()`
and pydantic's errors. `Annotated[T, Secret()]` marks any other type secret; `DocuconfSettings` hides it in
`repr()` and `str()`, but `model_dump()` returns the real value. Inside a nested model, use `SecretStr`.

**Item bounds** on an `int` list come from constraints on the item type, as pydantic writes them: `ge`/`le` (or
`gt`/`lt`, ±1) on `list[Annotated[int, Field(...)]]` or `list[conint(...)]`. They are exported as `itemMin` and
`itemMax`, and an item outside them is `out_of_range` at boot. Python's `int` holds any 64-bit value, so no bounds are
added on its own.

**Length limits** count characters (Unicode code points, Python's `len`), never bytes: `日本` is 2 and an emoji is 1.
For apps that store values in fixed-width fields, `Field(max_length=...)` bounds a URL as given (declare it as
`str` or `SecretStr` with `Url()`: pydantic measures an `AnyUrl` after normalising it, so that is a declaration
error), `min_length`/`max_length` on the item type bound each item of a string list after it is split, and
`docuconf.JsonMaxLength(n)` bounds a `json` variable, measured as received (whitespace included, before parsing) or,
from an overlay or a default, as the compact JSON the platform renders. A value outside them is `out_of_range`, and
a secret's message gives its length, never its value. Item lengths on an `int` list, a minimum above the maximum,
and a minimum length on a URL are declaration errors.

**Encodings** (SPEC §5) are the ones pydantic-settings parses natively: lists as JSON (`["a","b"]`) unless the field
uses `CsvList` (or `NoDecode` with `docuconf.Csv`), and durations as ISO 8601 (`PT90S`) unless the field carries
`docuconf.Duration("go")` (`1m30s`), `Duration("seconds")` (`90`) or `Duration("timespan")` (`00:01:30`). A wrong
duration names the expected form (`expected an ISO 8601 duration like PT30S`). A duration default may be a
`timedelta` or a string in the field's own encoding. A list marked `docuconf.IndexedList()` is read from `NAME__0`,
`NAME__1`..., which pydantic-settings cannot do, so docuconf gathers the items itself; they must be numbered from 0
with no gap (`NAME__0` and `NAME__2` without `NAME__1` is `invalid_type`), and other suffixes such as `NAME__HOST`
are not items. Platform authors still write `"90s"` and `["a", "b"]`; the platform's renderer converts. Duration
defaults and bounds are exported in canonical Go form (`1h30m`).

**Patterns** are exported as written. By default pydantic matches `pattern` with the Rust `regex` crate. Like RE2, it
has no lookaround or backreferences and matches anywhere in the value, as CUE's `=~` does, so anchor with `^...$` to
match the whole value. docuconf rejects non-RE2 syntax at declaration time, including under
`regex_engine="python-re"`, which also gets a warning because Python's `$` and `\d` behave differently. `\d` and
`\w` are Unicode-aware in pydantic but ASCII-only in RE2.

**Declaration checks** raise `docuconf.DeclarationError` from `load`, `Settings()`, `to_contract` and the CLI. They
cover a class that does not subclass `DocuconfSettings`, the env name format, descriptions of at least 5
characters, defaults that satisfy their own constraints (including enum values and bounds), defaults or examples on
secrets, markers on a type they do not fit (`Url()` on an `int`, `Csv()` on a `str`, `Duration()` on an `int`),
non-RE2 patterns, `Csv` without `NoDecode`, file input names and paths, a `path_env` that is also a variable, and
keystore password variables that are not declared secrets. Warnings (`docuconf.DocuconfWarning`, issued by `load`
and printed by the CLI) cover names that look like feature flags (`FF_`, `FEATURE_`, `ENABLE_`; see SPEC §10),
exclusive float bounds, and types exported as strings.

**Service name**: `metadata.name` is `docuconf_service` on the class, else the class name in kebab-case without a
`Settings` or `Config` suffix (`OrdersSettings` is `orders`). A class named just `Settings` needs
`docuconf_service` or `--name`.

## At boot

- Every violation carries a SPEC §11.2 code: `missing_required`, `invalid_type`, `out_of_range`, `pattern_mismatch`,
  `not_in_enum`, `invalid_scheme`, `too_few_items`, `too_many_items`, `file_missing`, `file_unreadable`,
  `file_too_large`, `file_malformed`, `schema_mismatch`, `certificate_invalid`, `certificate_expiring`,
  `certificate_name_mismatch`, `key_mismatch`, `keystore_unreadable`. `err.violations` lists them as
  `Violation(input, kind, code, message)`, and `err.codes` the codes.
- Secret values are never printed. For a secret variable, docuconf keeps only pydantic messages that do not echo
  the input; anything else becomes `invalid value (secret, not shown)`. Messages from your own validators (field or
  model) are scrubbed of every secret value before they are shown or written to the termination log.
- An empty value means *unset* for every type except `string` (SPEC §5): a defaulted variable takes its default, and
  a required one is `missing_required`. Values are never trimmed. Integers, including the items of an `int` list,
  must fit in 64 bits (`out_of_range` otherwise), and floats must be finite. A `json`-encoded `int` list must hold
  JSON integers (`[1,2]`, not `["1","2"]`). Malformed JSON is `invalid_type`, reported with every other problem.
- Sources and precedence are pydantic-settings': constructor arguments, then environment variables, then a `.env`
  file if you set `env_file` (opt-in; real environment variables win), then config-file overlays, then any config
  files you add in `settings_customise_sources`, then defaults. docuconf reads the environment once and hands
  pydantic-settings that snapshot through `settings_customise_sources`; it never changes `os.environ`.
- Variables not in the declaration are ignored, apart from the typo warning. Setting a deprecated variable logs a
  warning.

## Config-file overlays

pydantic-settings layers sources through `settings_customise_sources`, including JSON, YAML and TOML files. An
overlay (SPEC §4.7) is one more such file that the platform mounts from a ConfigMap, between the files the app ships
with and the environment:

```text
defaults < baked-in config files < overlay < .env and environment variables < constructor arguments
```

Declare overlays on the class. `DocuconfSettings` loads them with pydantic-settings' default sources. To add
baked-in files, pass your sources through `docuconf.with_overlays`, which inserts pydantic-settings' own
`JsonConfigSettingsSource`, `YamlConfigSettingsSource` or `TomlConfigSettingsSource` for each overlay just before
the first config file source (or last, when there is none):

```python
# catalog/settings.py
from collections.abc import Sequence
from typing import ClassVar

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

import docuconf
from docuconf import DocuconfSettings, Overlay


class Settings(DocuconfSettings):
    docuconf_service: ClassVar[str] = "catalog"
    docuconf_overlays: ClassVar[Sequence[Overlay]] = (
        Overlay("platform", "/app/config/catalog.json", reload="watch", description="Platform overrides"),
    )
    model_config = SettingsConfigDict(env_prefix="CATALOG_", env_nested_delimiter="__")

    page_size: int = Field(20, ge=1, description="Items per page")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return docuconf.with_overlays(
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
            YamlConfigSettingsSource(settings_cls, yaml_file="catalog.base.yaml"),  # shipped in the image
        )
```

- **Format**: `json`, `yaml` (needs the `yaml` extra) or `toml`, inferred from the extension or set with `format=`.
- **Keys**: the file nests values by field path, as pydantic-settings' file sources read them. The contract exports
  each non-secret variable's `configKey` as that path joined with `.` (field names, or aliases where set), with
  `keySeparator: "."`: `CATALOG_PAGE_SIZE` is `page_size`, and with `env_nested_delimiter="__"`,
  `CATALOG_SEARCH__URL` is `search.url`, so the platform writes `{"search": {"url": ...}}`. A nested model without
  `env_nested_delimiter` is one `json` variable, written as an object at its field name. `Meta(config_key=...)` must
  match the path when the class declares overlays.
- **Loading**: the file is optional; a missing one adds nothing. Its values are checked like env values, and
  violations say `(from overlay platform)`. A malformed file is `file_malformed`, reported with every other problem.
  A value set both in the environment and in an overlay logs a warning; the environment wins.
- **`reload="watch"`**: the watcher polls the file (Kubernetes swaps a symlink when a ConfigMap changes). When it
  changes, docuconf re-validates the whole class through pydantic-settings and replaces the changed fields on the
  settings object; a reload that fails its checks is logged and the previous values are kept. `on_reload` listeners
  receive the overlay's name and a dict of the changed fields. Code that copied a value out of the settings object
  keeps the old one, so read it from the settings object when you need it.
- **Declaration checks**: `load` raises `DeclarationError` when the class declares overlays but its sources do not go
  through `with_overlays`, when an environment source comes after a config file source, or when the overlay's
  directory would hide files the app ships with: the working directory, a baked-in config file's directory, or the
  module that declares the settings. Mounting the overlay's directory hides whatever the image has there, so give it
  a directory of its own, such as `/app/config`.

## Injected secrets

Platforms often supply secrets when the container starts rather than in the pod spec: Bank-Vaults' `vault-env`
resolves `vault:secret/data/db#url`, and wrappers such as `op run` resolve `op://...` references, before the app
starts. docuconf needs nothing for this: it reads the environment as the process sees it, after injection, validates
injected values like any other, and never resolves references itself (SPEC §4.5.1).

If the injector did not run, a secret variable still holds the raw reference. docuconf reports a secret whose value
starts with `vault:`, `op://` or `ref+` as `invalid_type`, naming the variable and the scheme but never the value:

```text
  - DATABASE_URL [invalid_type]: holds an unresolved vault: reference; the injector that should resolve it did not run
```

## Contract-first mode

Without a settings class, `docuconf.load_contract` validates an environment against a contract given as JSON (export
a hand-written `contract.cue` with `cue export contract.cue`) and returns the typed values, one attribute per
variable:

```python
# contract_first.py
import docuconf

values = docuconf.load_contract("contract.json", env={"PORT": "8080"})  # env= defaults to os.environ
print(values.PORT)
```

It reads every encoding in SPEC §5: lists as `csv` (with `separator`), `json` or `indexed` (`NAME__0`, `NAME__1`...,
numbered from 0 with no gap), and durations as `go`, `iso8601`, `seconds` or `timespan`. docuconf builds a
pydantic-settings class from the contract (`docuconf.contract_settings`) with the same constraints and markers a
hand-written declaration would use, and loads it through the same checks as `docuconf.load`, so the two modes cannot
drift apart. Violations raise `ConfigValidationError` and go to the termination log as usual. `json` variables are
checked against their `schema` with the `jsonschema` extra; without it, schemas are not checked and a warning is
logged. The mode loads variables only: a contract with `files` or `overlays` is a `DeclarationError`. Variable names
are matched case-sensitively.

## Conformance

`tests/test_conformance.py` runs the shared conformance suite from docuconf-go (`conformance/cases.json`, SPEC §12)
through the contract-first mode and reports each failing case by its `id`:

```sh
DOCUCONF_CONFORMANCE=../docuconf-go/conformance/cases.json DOCUCONF_REQUIRE_CONFORMANCE=1 pytest tests/test_conformance.py
```

Without `DOCUCONF_CONFORMANCE` it looks for `../docuconf-go/conformance/cases.json` and skips when the file is
missing, unless `DOCUCONF_REQUIRE_CONFORMANCE=1` (as in CI) makes that a failure. The SDK supports both capability
tags, `int64` (Python's `int` holds every 64-bit value) and `json-schema` (with the `jsonschema` package installed),
so no case is skipped. Without `jsonschema`, the two `json-schema` cases are skipped.

## Not supported yet

- JKS keystores (PKCS#12 only).
- Profiles (SPEC §4.4). Values in baked-in config files are not exported as defaults, so give fields defaults in
  Python if the platform need not set them.
- File inputs and overlays in the contract-first mode, which loads variables only.
- Markdown documentation generation.
- `AliasPath` aliases. A nested model without `env_nested_delimiter` is exported as one `json` variable.

## Development

```sh
uv venv && uv pip install -e '.[tls]' --group dev
pytest
ruff check . && ruff format --check . && mypy
UPDATE_GOLDEN=1 pytest tests/test_export.py    # after an intended change to the sample export
```

`tests/test_readme.py` runs the snippets in this README: the Python blocks headed with a file name become a small
project, whose tests, entry point, export commands and outputs are checked against the text here.

The export tests run `cue vet -c` on generated contracts against the meta-schema in
[docuconf-go](https://github.com/docuconf/docuconf-go) (`spec/cue`). They look for it in `../docuconf-go/spec/cue`
or `$DOCUCONF_SPEC_CUE`, and for `cue` in `$CUE`, `~/go/bin/cue` or `PATH`; without them, those tests are skipped.
The conformance runner reads the same checkout (see [Conformance](#conformance)).
See [RELEASING.md](RELEASING.md) for publishing.

## Licence

MIT. See [LICENSE](LICENSE).
