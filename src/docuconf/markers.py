"""Annotations that add docuconf metadata to pydantic-settings fields.

They go in ``typing.Annotated`` next to the type, beside pydantic's own
``Field``::

    database_url: Annotated[str, Secret(), Url(schemes=("postgres", "postgresql"))]
    origins: Annotated[list[str], NoDecode, Csv()]
    tls: Annotated[TlsKeyPair, TlsFile(path="/etc/app/tls", dns_names=("app.internal",))]
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from pydantic import GetCoreSchemaHandler, SecretStr
from pydantic_core import PydanticCustomError, core_schema

_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+$")


@dataclass(frozen=True)
class Secret:
    """Mark a field as secret (SPEC §6) when its type is not ``SecretStr``.

    The platform must supply it from a Secret, and docuconf never prints its
    value. Prefer ``SecretStr``, which also keeps the value out of ``repr``.
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
            s = v.get_secret_value() if isinstance(v, SecretStr) else str(v)
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


Reload = Literal["restart", "watch"]


@dataclass(frozen=True)
class FileInput:
    """Fields common to every file input (SPEC §4.6)."""

    #: Where the app reads the input: a directory for TLS, a file otherwise.
    path: str
    #: Input name (a DNS label). Defaults to the field name in kebab-case.
    name: str | None = None
    #: An environment variable the platform sets to ``path``.
    path_env: str | None = None
    #: ``restart`` (read once) or ``watch`` (docuconf reloads it; see ``docuconf.get_watcher``).
    reload: Reload = "restart"
    #: Upper bound in bytes.
    max_size: int | None = None
    #: Defaults to whether the pydantic field is required.
    required: bool | None = None
    group: str | None = None


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
