# docuconf-pydantic

Typed configuration contracts for [pydantic-settings](https://github.com/pydantic/pydantic-settings).

**Example:** [`examples/orders/`](examples/orders/), a small `http.server` service with its exported contract.

[docuconf](https://github.com/docuconf/docuconf-go/blob/main/spec/SPEC.md) treats an application's configuration
(environment variables, config files, TLS certificates, CA bundles, keystores) as an API between the app and the
Kubernetes platform that runs it. This package lets a Python service:

1. **Declare** its inputs with the `BaseSettings` class it already has. Descriptions come from
   `Field(description=...)`, constraints from `ge`/`le`/`min_length`/`max_length`/`pattern`, enums from `Literal`
   or `Enum`, secrets from `SecretStr`. docuconf adds only what pydantic cannot express: a secret marker for types
   other than `SecretStr`, URL schemes, CSV lists and file inputs.
2. **Export** that declaration as a CUE contract (`contract.cue`), which the platform validates its values against
   before deploying.
3. **Validate at boot**: pydantic-settings loads and parses as usual, and docuconf checks the rest (file inputs, TLS
   material, the spec's parsing rules), then reports *every* problem at once with a stable error code.

## Install

```sh
pip install docuconf-pydantic            # Python 3.10+
pip install 'docuconf-pydantic[yaml]'    # for YAML config files (PyYAML)
pip install 'docuconf-pydantic[jsonschema]'  # contract-first mode: check json variables against their schema
```

The distribution is `docuconf-pydantic`; the import package is `docuconf`.

## Example

```python
# orders/settings.py
from datetime import timedelta
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from docuconf import ConfigFile, Csv, TlsFile, TlsKeyPair, Url


class Rates(BaseModel):
    per_minute: int = Field(ge=1)
    burst: int = Field(0, ge=0)


class Settings(BaseSettings):
    docuconf_service: ClassVar[str] = "orders"  # metadata.name in the contract
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

    # File inputs: a TLS key pair (kubernetes.io/tls layout) and a JSON file bound to the Rates model.
    tls: Annotated[
        TlsKeyPair,
        TlsFile(path="/etc/orders/tls", dns_names=("orders.internal",), min_remaining="720h", reload="watch"),
    ] = Field(description="Certificate the service serves HTTPS with")
    rates: Annotated[Rates, ConfigFile(path="/etc/orders/rates/rates.json", path_env="ORDERS_RATES_FILE")] = Field(
        description="Per-client rate limits"
    )
```

At boot:

```python
import docuconf
from orders.settings import Settings

settings = docuconf.load(Settings)  # raises docuconf.ConfigValidationError listing every problem
ssl_context = settings.tls.server_context()  # reloads its certificate when the files rotate
print(settings.rates.per_minute)
```

A failed boot reports everything at once, without secret values:

```text
docuconf.errors.ConfigValidationError: docuconf: 4 configuration problems:
  - ORDERS_DATABASE_URL [invalid_scheme]: URL scheme should be one of 'postgres', 'postgresql'
  - ORDERS_PORT [out_of_range]: Input should be less than or equal to 65535 (got '70000')
  - rates [schema_mismatch]: per_minute: Input should be greater than or equal to 1
  - tls [certificate_expiring]: certificate expires at 2026-10-20T09:00:00Z, less than 720h from now
```

Export the contract in CI, and commit or publish it with the image:

```sh
docuconf export orders.settings:Settings --out contract.cue
docuconf export orders.settings:Settings --out contract.cue --check   # fail if it is out of date
docuconf export orders.settings:Settings --name orders --app-version "$GIT_SHA"
```

From Python, `docuconf.to_contract(Settings)` returns the CUE text and `docuconf.contract_data(Settings)` the same
contract as plain data. The contract exported for this example is
[`examples/app/contract.cue`](examples/app/contract.cue). If you prefer a classmethod, the `DocuconfSettings` mixin
adds `Settings.load()`.

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
| `Literal["a", "b"]`, `Enum` of strings | `enum` |
| `list[str]`, `list[int]` (`min_length`, `max_length`) | `list` (`minItems`, `maxItems`), `encoding: "json"` |
| `list[Annotated[int, Field(ge=0, le=1023)]]`, `list[conint(ge=0)]` | `list` of `int` with `itemMin`, `itemMax` |
| `Annotated[list[str], NoDecode, Csv(";")]` | `list`, `encoding: "csv"`, `separator: ";"` |
| `Annotated[list[str], IndexedList()]` | `list`, `encoding: "indexed"` (`NAME__0`, `NAME__1`...) |
| a model, a `dict`, a list of models... | `json`, with `schema` from pydantic's JSON Schema |
| a nested model with `env_nested_delimiter` | one variable per field, e.g. `APP_DB__HOST` |
| `SecretStr`, or `Annotated[T, Secret()]` | `secret: true` |
| `Field(examples=...)`, `Field(deprecated=...)` | `examples`, `deprecated` |
| `Annotated[T, Meta(group=..., replaced_by=..., config_key=...)]` | `group`, `deprecated.replacedBy`, `configKey` |
| `Annotated[T, Exclude()]` | left out (for values from a secrets manager, say) |

**Env names** follow pydantic-settings' own rules: `env_prefix` plus the field name, or the field's `alias` /
`validation_alias` (the first `AliasChoices` entry is exported; the others still work at boot). Contracts use
upper-case names. pydantic-settings matches names case-insensitively by default, so `port` and `PORT` both work at
boot. With `case_sensitive=True`, the names in your class must already be upper-case.

**Item bounds** on an `int` list come from constraints on the item type, as pydantic writes them: `ge`/`le` (or
`gt`/`lt`, ±1) on `list[Annotated[int, Field(...)]]` or `list[conint(...)]`. They are exported as `itemMin` and
`itemMax`, and an item outside them is `out_of_range` at boot. Python's `int` holds any 64-bit value, so no bounds are
added on its own.

**Encodings** (SPEC §5) are the ones pydantic-settings parses natively: lists as JSON (`["a","b"]`) unless the field
uses `NoDecode` with `docuconf.Csv`, and durations as ISO 8601 (`PT90S`) unless the field carries
`docuconf.Duration("go")` (`1m30s`), `Duration("seconds")` (`90`) or `Duration("timespan")` (`00:01:30`). A list
marked `docuconf.IndexedList()` is read from `NAME__0`, `NAME__1`..., which pydantic-settings cannot do, so docuconf
gathers the items itself; they must be numbered from 0 with no gap (`NAME__0` and `NAME__2` without `NAME__1` is
`invalid_type`), and other suffixes such as `NAME__HOST` are not items. Platform authors still write `"90s"` and
`["a", "b"]`; the platform's renderer converts. Duration defaults and bounds are exported in canonical Go form
(`1h30m`).

**Patterns** are exported as written. By default pydantic matches `pattern` with the Rust `regex` crate. Like RE2, it
has no lookaround or backreferences and matches anywhere in the value, as CUE's `=~` does, so anchor with `^...$` to
match the whole value. docuconf rejects non-RE2 syntax at declaration time, including under
`regex_engine="python-re"`, which also gets a warning because Python's `$` and `\d` behave differently. `\d` and
`\w` are Unicode-aware in pydantic but ASCII-only in RE2.

**Declaration checks** raise `docuconf.DeclarationError` from `load`, `to_contract` and the CLI. They cover the env
name format, descriptions of at least 5 characters, defaults that satisfy their own constraints, defaults or
examples on secrets, non-RE2 patterns, `Csv` without `NoDecode`, file input names and paths, a `path_env` that is also
a variable, and keystore password variables that are not declared secrets. Warnings (logged at debug level by
`load`, printed by the CLI) cover names that look like feature flags (`FF_`, `FEATURE_`, `ENABLE_`; see SPEC §10),
exclusive float bounds, and types exported as strings.

## File inputs

A file input is a field annotated with a file marker. docuconf reads and checks the file, then passes the value to
the settings constructor, so the field has a typed value like any other.

| marker | field type | checked at boot |
|---|---|---|
| `ConfigFile(path, format=json\|yaml\|toml)` | the model it binds to | parses (a UTF-8 byte-order mark is accepted) and validates against the model |
| `TlsFile(path, dns_names, key_algorithms, min_remaining, require_ca)` | `TlsKeyPair` | `tls.crt` and `tls.key` parse and match; validity and `min_remaining`; SAN DNS names (a wildcard covers one label); key algorithm; chain order; chain to `ca.crt` |
| `CaBundleFile(path, min_certificates)` | `CaBundle` | at least `min_certificates` parseable PEM certificates |
| `KeystoreFile(path, password_var)` | `Keystore` | the PKCS#12 file opens with the password variable |
| `TextFile(path, pattern, min_length, max_length)` | `str` (the content) or `Path` | UTF-8, length, pattern |
| `BinaryFile(path)` | `bytes` (the content) or `Path` | exists, readable, within `max_size` |

Every marker also takes `name` (by default the field name in kebab-case), `path_env`, `reload` (`restart` or
`watch`), `max_size`, `required` (by default, whether the field is required) and `group`. An optional file input
looks like `Annotated[CaBundle | None, CaBundleFile(...)] = None`. TLS and keystore inputs are always secret; mark
other secret files with `Secret()`.

TLS checks use the `cryptography` package. The chain to `ca.crt` is verified with `cryptography.x509.verification`
(cryptography 45 or newer) under a permissive extension policy, since private CAs rarely follow the Web PKI profile;
CA certificates must still carry basicConstraints. With older cryptography, the SDK falls back to checking that the
top of `tls.crt` is, or is directly issued by, a certificate in `ca.crt`.

**`reload="watch"`**: `docuconf.load` starts a daemon thread that polls each watched input every 2 seconds
(`watch_interval=` changes this). Kubernetes updates projected volumes by swapping a symlink, so the watcher stats
every file of the input through symlinks and re-reads all of them together when any has changed. A reload that fails
its checks is logged and the old value is kept. `TlsKeyPair`, `CaBundle` and `Keystore` values are updated in place,
and every `SSLContext` made by `TlsKeyPair.server_context()` or `client_context()` loads the new certificate. Other
values are replaced on the settings object. Listen with `pair.on_change(callback)` or
`docuconf.get_watcher(settings).on_reload(callback)`, and stop with `docuconf.get_watcher(settings).stop()`.

## At boot

- Every violation carries a SPEC §11.2 code: `missing_required`, `invalid_type`, `out_of_range`, `pattern_mismatch`,
  `not_in_enum`, `invalid_scheme`, `too_few_items`, `too_many_items`, `file_missing`, `file_unreadable`,
  `file_too_large`, `file_malformed`, `schema_mismatch`, `certificate_invalid`, `certificate_expiring`,
  `certificate_name_mismatch`, `key_mismatch`, `keystore_unreadable`. `err.violations` lists them as
  `Violation(input, kind, code, message)`.
- Secret values are never printed. For a secret variable, docuconf keeps only pydantic messages that do not echo
  the input; anything else, such as a custom validator's message, becomes `invalid value (secret, not shown)`.
- The message is also written to `/dev/termination-log` when it exists, so `kubectl describe pod` shows it.
  `DOCUCONF_TERMINATION_LOG` overrides the path, and `load(..., termination_log=False)` turns it off.
- An empty value means *unset* for every type except `string` (SPEC §5): a defaulted variable takes its default, and
  a required one is `missing_required`. Values are never trimmed. Integers, including the items of an `int` list,
  must fit in 64 bits (`out_of_range` otherwise), and floats must be finite. A `json`-encoded `int` list must hold
  JSON integers (`[1,2]`, not `["1","2"]`). Malformed JSON is `invalid_type`, reported with every other problem.
- `DOCUCONF_FILE_ROOT=/some/dir` is prepended to every absolute file path, including paths read from a `path_env`
  variable. Use it for local development and tests.
- Sources and precedence are pydantic-settings': constructor arguments, then environment variables, then a `.env`
  file if you set `env_file` (opt-in; real environment variables win), then config-file overlays, then any config
  files you add in `settings_customise_sources`, then defaults. docuconf reads the environment the same way for its
  own checks.
- Variables not in the declaration are ignored. Setting a deprecated variable logs a warning.

## Config-file overlays

pydantic-settings layers sources through `settings_customise_sources`, including JSON, YAML and TOML files. An
overlay (SPEC §4.7) is one more such file that the platform mounts from a ConfigMap, between the files the app ships
with and the environment:

```text
defaults < baked-in config files < overlay < .env and environment variables < constructor arguments
```

Declare overlays on the class, and pass your sources through `docuconf.with_overlays`, which inserts pydantic-settings'
own `JsonConfigSettingsSource`, `YamlConfigSettingsSource` or `TomlConfigSettingsSource` for each overlay just before
the first config file source (or last, when there is none):

```python
from collections.abc import Sequence
from typing import ClassVar

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

import docuconf
from docuconf import Overlay


class Settings(BaseSettings):
    docuconf_overlays: ClassVar[Sequence[Overlay]] = (
        Overlay("platform", "/app/config/catalog.json", reload="watch", description="Platform overrides"),
    )
    model_config = SettingsConfigDict(env_prefix="CATALOG_", env_nested_delimiter="__")
    ...

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


settings = docuconf.load(Settings)
```

Without baked-in files, mixing in `docuconf.DocuconfSettings` does the same with pydantic-settings' default sources.

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
values = docuconf.load_contract("contract.json")  # or a dict, or JSON text; env= defaults to os.environ
print(values.PORT, values.TIMEOUT)  # int, timedelta
```

It reads every encoding in SPEC §5: lists as `csv` (with `separator`), `json` or `indexed` (`NAME__0`, `NAME__1`...,
numbered from 0 with no gap), and durations as `go`, `iso8601`, `seconds` or `timespan`. docuconf builds a pydantic-settings class from the
contract (`docuconf.contract_settings`) with the same constraints and markers a hand-written declaration would use,
and loads it through the same checks as `docuconf.load`, so the two modes cannot drift apart. Violations raise
`ConfigValidationError` and go to the termination log as usual. `json` variables are checked against their `schema`
with the `jsonschema` extra; without it, schemas are not checked and a warning is logged. The mode loads variables only: a
contract with `files` or `overlays` is a `DeclarationError`. Variable names are matched case-sensitively.

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
uv venv && uv pip install -e . --group dev     # or: pip install -e '.[jsonschema]' pytest PyYAML ruff mypy types-PyYAML
pytest
ruff check . && ruff format --check . && mypy
UPDATE_GOLDEN=1 pytest tests/test_export.py    # after an intended change to the sample export
```

The export tests run `cue vet -c` on generated contracts against the meta-schema in
[docuconf-go](https://github.com/docuconf/docuconf-go) (`spec/cue`). They look for it in `../docuconf-go/spec/cue`
or `$DOCUCONF_SPEC_CUE`, and for `cue` in `$CUE`, `~/go/bin/cue` or `PATH`; without them, those tests are skipped.
The conformance runner reads the same checkout (see [Conformance](#conformance)).
See [RELEASING.md](RELEASING.md) for publishing.

## Licence

MIT. See [LICENSE](LICENSE).
