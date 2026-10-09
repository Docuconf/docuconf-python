"""The shared export fixture (docuconf-go ``conformance/export/fixture.yaml``), declared with this SDK.

``tests/test_export.py`` exports it and compares the result with the golden
contract in docuconf-go (``docuconf conformance export --golden``). Each
description is the field's ``description``, and each details its attribute
docstring, as in an app.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Annotated, ClassVar, Literal

from pydantic import ConfigDict, Field, SecretStr
from pydantic_settings import NoDecode
from typing_extensions import NotRequired, TypedDict

from docuconf import (
    BinaryFile,
    CaBundle,
    CaBundleFile,
    ConfigFile,
    Csv,
    CsvList,
    DocuconfSettings,
    Duration,
    JsonMaxLength,
    Keys,
    KeySet,
    Keystore,
    KeystoreFile,
    Meta,
    TextFile,
    TlsFile,
    TlsKeyPair,
    Url,
)


class RateLimits(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")  # type: ignore[misc]

    perMinute: Annotated[int, Field(ge=1)]
    burst: NotRequired[Annotated[int, Field(ge=0)]]


class AppSettings(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")  # type: ignore[misc]

    name: Annotated[str, Field(min_length=1)]
    replicas: Annotated[int, Field(ge=1)]
    tags: NotRequired[list[str]]


Origin = Annotated[str, Field(min_length=1, max_length=255)]
Shard = Annotated[int, Field(ge=0, le=1023)]


class FixtureSettings(DocuconfSettings):
    docuconf_service: ClassVar[str] = "docuconf-fixture"

    app_name: Annotated[str, Meta(group="general", config_key="App:Name")] = Field(
        "orders",
        min_length=2,
        max_length=40,
        pattern=r"^[a-z][a-z0-9-]*$",
        examples=["orders", "billing"],
        description="Service name, used in logs and metrics",
    )
    """Lower case, as a DNS label allows."""
    database_url: Annotated[SecretStr, Url(schemes=("postgres", "postgresql")), Meta(group="database")] = Field(
        max_length=2048, description="Primary Postgres connection string"
    )
    port: int = Field(8080, ge=1, le=65535, description="HTTP listen port")
    trace_ratio: float = Field(0.25, ge=0, le=1, description="Fraction of requests traced")
    debug: bool = Field(False, description="Serve the debug endpoints")
    request_timeout: Annotated[timedelta, Duration("go")] = Field(
        timedelta(minutes=1, seconds=30),
        ge=timedelta(seconds=1),
        le=timedelta(minutes=5),
        description="Upstream request timeout",
    )
    log_level: Literal["debug", "info", "warn", "error"] = Field("info", description="Minimum log level")
    allowed_origins: Annotated[list[Origin], NoDecode, Csv(";")] | None = Field(
        None, min_length=1, max_length=5, description="CORS origins allowed to call the API"
    )
    shards: CsvList[Shard] | None = Field(None, description="Shards this instance owns")
    webhook_keys: Annotated[KeySet, Keys(key_min_length=32, key_max_length=256)] | None = Field(
        None, description="Keys that verify webhook signatures"
    )
    rate_limits: Annotated[RateLimits, JsonMaxLength(1024)] = Field(
        {"perMinute": 60}, description="Per-client rate limits"
    )
    old_port: Annotated[int | None, Meta(replaced_by="PORT")] = Field(
        None, deprecated="Use PORT instead", description="Port the service used to listen on"
    )
    partner_password: SecretStr | None = Field(None, description="Password of the partner keystore")

    settings: Annotated[
        AppSettings,
        ConfigFile(
            path="/etc/app/settings/settings.json",
            path_env="SETTINGS_FILE",
            reload="watch",
            max_size=65536,
            group="general",
        ),
    ] = Field(description="Application settings")
    rules: Annotated[AppSettings, ConfigFile(path="/etc/app/rules/rules.yaml")] | None = Field(
        None, description="Routing rules"
    )
    flags: Annotated[AppSettings, ConfigFile(path="/etc/app/flags/flags.toml")] | None = Field(
        None, description="Feature defaults"
    )
    serving_tls: (
        Annotated[
            TlsKeyPair,
            TlsFile(
                path="/etc/app/tls",
                reload="watch",
                dns_names=("app.example.test", "api.example.test"),
                key_algorithms=("ECDSA", "Ed25519"),
                min_remaining="720h",
                require_ca=True,
            ),
        ]
        | None
    ) = Field(None, description="Certificate the service serves HTTPS with")
    trust: Annotated[CaBundle, CaBundleFile(path="/etc/app/trust/bundle.pem", min_certificates=2)] | None = Field(
        None, description="CAs the service trusts"
    )
    partner: (
        Annotated[Keystore, KeystoreFile(path="/etc/app/partner/keystore.p12", password_var="PARTNER_PASSWORD")] | None
    ) = Field(None, description="Client certificate for the partner API")
    licence: (
        Annotated[
            str, TextFile(path="/etc/app/licence/licence.key", pattern=r"^[A-Z0-9-]+\n?$", min_length=8, max_length=64)
        ]
        | None
    ) = Field(None, description="Licence key")
    geoip: (
        Annotated[Path, BinaryFile(path="/data/geoip/geoip.mmdb", max_size=134217728), Meta(replaced_by="geo-db")]
        | None
    ) = Field(None, deprecated="Use geo-db instead", description="GeoIP database")
    geo_db: Annotated[Path, BinaryFile(path="/data/geo-db/geo.mmdb")] | None = Field(
        None, description="City-level location database"
    )
