"""Annotations that add docuconf metadata to pydantic-settings fields.

They go in ``typing.Annotated`` next to the type, beside pydantic's own
``Field``::

    database_url: Annotated[str, Secret(), Url(schemes=("postgres", "postgresql"))]
    origins: Annotated[list[str], NoDecode, Csv()]
    tls: Annotated[TlsKeyPair, TlsFile(path="/etc/app/tls", dns_names=("app.internal",))]
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any, Literal, TypeVar

from pydantic import GetCoreSchemaHandler
from pydantic_core import PydanticCustomError, core_schema
from pydantic_settings import NoDecode

from . import durations

log = logging.getLogger("docuconf")

_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+$")


@dataclass(frozen=True)
class Secret:
    """Mark a field as secret (SPEC §6) when its type is not ``SecretStr`` or ``pydantic.Secret[T]``.

    The platform must supply it from a Secret, and docuconf never prints its
    value: ``DocuconfSettings`` shows it as ``'**********'`` in ``repr()`` and
    ``str()``, and boot errors never contain it. ``model_dump()`` still returns
    the value, so prefer ``SecretStr`` or ``pydantic.Secret[T]`` for values you
    serialise. Inside a nested model, ``Secret()`` is a declaration error.
    """


@dataclass(frozen=True)
class Exclude:
    """Leave a field out of the contract (SPEC §4.4), e.g. a value read from a
    secrets manager the platform does not control. pydantic still loads it."""


@dataclass(frozen=True)
class Meta:
    """Contract metadata pydantic has no field for."""

    group: str | None = None
    #: For a deprecated variable (``Field(deprecated=...)``): its replacement.
    replaced_by: str | None = None
    #: The app's own configuration key, where it differs from the env name.
    config_key: str | None = None


@dataclass(frozen=True)
class Url:
    """A URL variable, optionally restricted to ``schemes``.

    Use it on ``str`` or ``SecretStr`` (pydantic's ``AnyUrl``, ``HttpUrl`` and
    ``PostgresDsn`` are recognised without it). Values must look like
    ``scheme://...``, as the contract's ``url`` type requires.
    """

    schemes: Sequence[str] = ()

    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        schemes = tuple(s.lower() for s in self.schemes)

        def check(v: Any) -> Any:
            if v is None:
                return v
            s = str(v.get_secret_value()) if hasattr(v, "get_secret_value") else str(v)
            if not _URL.match(s):
                raise PydanticCustomError("url_parsing", "Input should be a URL of the form scheme://...")
            scheme = s.split("://", 1)[0].lower()
            if schemes and scheme not in schemes:
                raise PydanticCustomError(
                    "url_scheme",
                    "URL scheme should be one of {expected}",
                    {"expected": ", ".join(repr(x) for x in schemes)},
                )
            return v

        return core_schema.no_info_after_validator_function(check, handler(source))


@dataclass(frozen=True)
class Csv:
    """Split a list variable on ``separator`` instead of decoding JSON.

    pydantic-settings decodes list values as JSON unless the field carries
    ``NoDecode``, so use both::

        hosts: Annotated[list[str], NoDecode, Csv()]

    The contract records ``encoding: "csv"`` and the separator, so the platform
    renders ``a,b`` rather than ``["a","b"]``.
    """

    separator: str = ","

    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        sep = self.separator

        def split(v: Any) -> Any:
            if isinstance(v, str):
                return [] if v == "" else v.split(sep)
            return v

        return core_schema.no_info_before_validator_function(split, handler(source))


_T = TypeVar("_T")

#: A comma-separated list variable: ``CsvList[str]`` or ``CsvList[int]``.
#: Short for ``Annotated[list[T], NoDecode, Csv()]``; use that form for another separator.
CsvList = Annotated[list[_T], NoDecode, Csv()]


@dataclass(frozen=True)
class Duration:
    """The wire encoding of a ``timedelta`` variable (SPEC §5).

    pydantic parses ISO 8601 (``PT90S``) natively, which is what a plain
    ``timedelta`` field exports. ``Duration("go")`` reads Go syntax
    (``1m30s``) instead, ``Duration("seconds")`` a decimal number of seconds
    and ``Duration("timespan")`` a .NET ``TimeSpan`` (``[d.]hh:mm:ss[.fff]``)::

        timeout: Annotated[timedelta, Duration("go")] = timedelta(seconds=30)

    Python's ``timedelta`` holds microseconds, so finer digits are dropped.
    """

    encoding: durations.Encoding = "iso8601"

    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        encoding = self.encoding
        if encoding == "iso8601":
            return handler(source)

        def parse(v: Any) -> Any:
            if not isinstance(v, str):
                return v
            try:
                return durations.parse(v, encoding)
            except ValueError:
                raise PydanticCustomError(
                    "duration_parsing", "Input should be a duration in {encoding} form", {"encoding": encoding}
                ) from None

        return core_schema.no_info_before_validator_function(parse, handler(source))


@dataclass(frozen=True)
class IndexedList:
    """A list read from ``NAME__0``, ``NAME__1``... (SPEC §5): ``Annotated[list[str], IndexedList()]``.

    pydantic-settings cannot read that form itself, so docuconf gathers the
    items and passes them to the settings class. Items must be numbered from 0
    with no gap; ``NAME__0`` and ``NAME__2`` without ``NAME__1`` is
    ``invalid_type``. ``NAME`` itself, and suffixes that are not an index
    (``NAME__HOST``), are not read.
    """


@dataclass(frozen=True)
class JsonValue:
    """A ``json`` variable checked against a JSON Schema, for the contract-first mode.

    The value is decoded from JSON text and, when ``schema`` is set, validated
    with the ``jsonschema`` package (the ``jsonschema`` extra). Without that
    package, the schema is exported but not checked, and a warning is logged.
    """

    # Compared and hashed by value: typing caches Annotated[...] by its arguments, so two markers that compared
    # equal would share one cached annotation. A dict schema is unhashable, which turns that cache off.
    schema: Mapping[str, Any] | None = None

    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        check = None
        if self.schema:
            try:
                check = schema_validator(self.schema)
            except ImportError:
                log.warning(
                    "docuconf: jsonschema is not installed, so json values are not checked against their schema; "
                    "pip install 'docuconf-pydantic[jsonschema]'"
                )

        def decode(v: Any) -> Any:
            if not isinstance(v, str):
                return v
            try:
                return json.loads(v, parse_constant=_reject_constant)
            except ValueError:
                raise PydanticCustomError("json_invalid", "Input should be valid JSON") from None

        def validate(v: Any) -> Any:
            if check is not None:
                errors = sorted(check.iter_errors(v), key=lambda e: list(e.absolute_path))
                if errors:
                    first = errors[0]
                    where = "/".join(str(p) for p in first.absolute_path) or "the value"
                    raise PydanticCustomError(
                        "schema_mismatch",
                        "{where} does not match the JSON Schema: {detail}",
                        {"where": where, "detail": first.message},
                    )
            return v

        return core_schema.no_info_after_validator_function(
            validate, core_schema.no_info_before_validator_function(decode, handler(source))
        )


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def schema_validator(schema: Mapping[str, Any]) -> Any:
    """A ``jsonschema`` validator for ``schema``; raises ``ImportError`` without the package."""
    import jsonschema

    cls = jsonschema.validators.validator_for(schema, default=jsonschema.Draft202012Validator)
    cls.check_schema(dict(schema))
    return cls(dict(schema))


Reload = Literal["restart", "watch"]


@dataclass(frozen=True)
class FileInput:
    """Fields common to every file input (SPEC §4.6)."""

    #: Where the app reads the input: a directory for TLS, a file otherwise.
    path: str
    #: Input name (a DNS label). Defaults to the field name in kebab-case.
    name: str | None = None
    #: An environment variable the platform sets to ``path`` (``pathEnv`` in the contract).
    path_env: str | None = None
    #: ``restart`` (read once) or ``watch`` (docuconf reloads it; see ``docuconf.get_watcher``).
    reload: Reload = "restart"
    #: Upper bound in bytes.
    max_size: int | None = None
    #: Defaults to whether the pydantic field is required.
    required: bool | None = None
    group: str | None = None
    #: Alias of ``path_env``, named like pydantic-settings' own options.
    env: str | None = None

    def __post_init__(self) -> None:
        if self.env is not None:
            if self.path_env is not None and self.path_env != self.env:
                raise TypeError(f"{type(self).__name__}: env= and path_env= name the same thing; pass one of them")
            object.__setattr__(self, "path_env", self.env)


@dataclass(frozen=True)
class ConfigFile(FileInput):
    """A structured config file bound to the field's type (a pydantic model).

    Its JSON Schema (``model_json_schema()``) goes into the contract. YAML needs
    the ``yaml`` extra (PyYAML).
    """

    #: ``json``, ``yaml`` or ``toml``; inferred from the file extension when omitted.
    format: Literal["json", "yaml", "toml"] | None = None


KeyAlgorithm = Literal["RSA", "ECDSA", "Ed25519"]


@dataclass(frozen=True)
class TlsFile(FileInput):
    """A key pair in the ``kubernetes.io/tls`` layout: ``tls.crt``, ``tls.key``
    and, with ``require_ca``, ``ca.crt``. Field type: :class:`docuconf.TlsKeyPair`."""

    dns_names: Sequence[str] = ()
    key_algorithms: Sequence[KeyAlgorithm] = ()
    #: Least validity left, as a Go duration (``"720h"``) or a ``timedelta``.
    min_remaining: str | timedelta | None = None
    require_ca: bool = False


@dataclass(frozen=True)
class CaBundleFile(FileInput):
    """A PEM file of CA certificates. Field type: :class:`docuconf.CaBundle`."""

    min_certificates: int = 1


@dataclass(frozen=True)
class KeystoreFile(FileInput):
    """A PKCS#12 keystore. Field type: :class:`docuconf.Keystore`.

    ``password_var`` names a declared secret variable holding the password.
    """

    password_var: str | None = None
    format: Literal["pkcs12"] = "pkcs12"


@dataclass(frozen=True)
class TextFile(FileInput):
    """A text file such as a licence key. Field type: ``str`` (the content) or ``Path``."""

    #: RE2 pattern, matched anywhere in the content (anchor with ^ and $).
    pattern: str | None = None
    min_length: int | None = None
    max_length: int | None = None


@dataclass(frozen=True)
class BinaryFile(FileInput):
    """Opaque bytes. Field type: ``bytes`` (the content) or ``Path`` (just the location)."""


@dataclass(frozen=True)
class Overlay:
    """A config-file overlay (SPEC §4.7): a file the platform mounts, layered
    between the app's baked-in config files and environment variables.

    Declare overlays on the settings class and load them with
    :func:`docuconf.with_overlays` in ``settings_customise_sources``::

        class Settings(BaseSettings):
            docuconf_overlays: ClassVar[Sequence[Overlay]] = (
                Overlay("platform", "/app/config/settings.yaml", reload="watch"),
            )

    The file holds each value at its field path (``db.port`` is
    ``{"db": {"port": 5432}}``), which the contract exports as ``configKey``
    with ``keySeparator: "."``. It is optional: a missing file adds nothing.
    """

    #: Overlay name in the contract (a DNS label).
    name: str
    #: Where the platform mounts the file. Its directory must not hold files the app ships with.
    path: str
    #: ``json``, ``yaml`` or ``toml``; inferred from the file extension when omitted.
    format: Literal["json", "yaml", "toml"] | None = None
    #: ``restart`` (read once) or ``watch`` (docuconf reloads it; see ``docuconf.get_watcher``).
    reload: Reload = "restart"
    description: str | None = None


FILE_MARKERS = (ConfigFile, TlsFile, CaBundleFile, KeystoreFile, TextFile, BinaryFile)

__all__ = [
    "BinaryFile",
    "CaBundleFile",
    "ConfigFile",
    "Csv",
    "CsvList",
    "Duration",
    "Exclude",
    "FileInput",
    "KeystoreFile",
    "Meta",
    "Overlay",
    "Secret",
    "TextFile",
    "TlsFile",
    "Url",
]
