"""The sample declaration: every variable type and every file input type."""

from __future__ import annotations

from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, PostgresDsn, SecretStr
from pydantic_settings import NoDecode, SettingsConfigDict

from docuconf import (
    BinaryFile,
    CaBundle,
    CaBundleFile,
    ConfigFile,
    Csv,
    DocuconfSettings,
    Keystore,
    KeystoreFile,
    Meta,
    TextFile,
    TlsFile,
    TlsKeyPair,
    Url,
)


class LogLevel(str, Enum):
    debug = "debug"
    info = "info"
    warn = "warn"
    error = "error"


class RateLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    perMinute: int = Field(ge=1)
    burst: int = Field(0, ge=0)


class Route(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match: str = Field(pattern=r"^/")
    upstream: str = Field(pattern=r"^https?://")
    timeout: str | None = None


class Routes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    routes: list[Route] = Field(min_length=1)


class GatewaySettings(DocuconfSettings):
    docuconf_service: ClassVar[str] = "sample-gateway"
    model_config = SettingsConfigDict(env_prefix="")

    allowed_origins: Annotated[list[str], NoDecode, Csv()] = Field(
        description="CORS origins allowed to call the API", min_length=1, max_length=10
    )
    database_url: Annotated[SecretStr, Url(schemes=("postgres", "postgresql"))] = Field(
        description="Primary Postgres connection string"
    )
    debug: bool = Field(False, description="Verbose request logging")
    gomemlimit: int | None = Field(None, description="Soft memory limit, in bytes", ge=1)
    keystore_password: SecretStr = Field(description="Password for the partner keystore", min_length=1)
    log_level: Annotated[LogLevel, Meta(group="logging")] = Field(
        LogLevel.info, description="Minimum log level emitted"
    )
    mode: Literal["active", "standby"] = Field("active", description="Replication role of this instance")
    port: int = Field(8080, description="HTTP listen port", ge=1, le=65535)
    public_url: Annotated[str, Url(schemes=("https",))] = Field(description="Externally visible base URL")
    rate_limits: RateLimits | None = Field(None, description="Default per-client rate limits")
    region: str = Field(
        description="Cloud region the service runs in",
        min_length=4,
        max_length=32,
        pattern=r"^[a-z]{2}-[a-z]+-[0-9]$",
        examples=["eu-west-1"],
    )
    replica_dsn: PostgresDsn | None = Field(None, description="Read replica connection string")
    request_timeout: timedelta = Field(
        timedelta(seconds=30),
        description="Upstream request timeout",
        ge=timedelta(seconds=1),
        le=timedelta(minutes=5),
    )
    sample_rate: float = Field(0.25, description="Fraction of requests traced", ge=0, le=1)
    worker_ports: list[Annotated[int, Field(ge=1, le=65535)]] = Field(
        default_factory=list, description="Ports the workers bind"
    )

    # File inputs
    routes: Annotated[
        Routes,
        ConfigFile(path="/etc/gateway/routes/routes.yaml", path_env="ROUTES_FILE", reload="watch", max_size=65536),
    ] = Field(description="Routing table: path prefixes and their upstreams")
    serving_tls: Annotated[
        TlsKeyPair,
        TlsFile(
            path="/etc/gateway/tls",
            dns_names=("gateway.internal", "api.example.com"),
            key_algorithms=("ECDSA", "RSA"),
            min_remaining="720h",
            require_ca=True,
            reload="watch",
        ),
    ] = Field(description="Certificate the gateway serves HTTPS with")
    upstream_ca: Annotated[
        CaBundle | None, CaBundleFile(path="/etc/gateway/ca/bundle.pem", path_env="SSL_CERT_FILE")
    ] = Field(None, description="Private CAs the gateway trusts for upstream TLS")
    partner_keystore: Annotated[
        Keystore | None,
        KeystoreFile(path="/etc/gateway/partner/keystore.p12", password_var="KEYSTORE_PASSWORD"),
    ] = Field(None, description="Client certificate for mTLS to the partner API")
    license: Annotated[
        str, TextFile(path="/etc/gateway/license/license.key", pattern=r"^[A-Z0-9]{5}(-[A-Z0-9]{5}){3}\n?$")
    ] = Field(description="Gateway licence key")
    geoip: Annotated[Path | None, BinaryFile(path="/data/geoip/GeoLite2-City.mmdb", max_size=134217728)] = Field(
        None, description="GeoIP database for country-based routing"
    )
