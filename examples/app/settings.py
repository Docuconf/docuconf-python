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
        max_length=2048, description="Primary Postgres connection string"
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
