"""A declaration with a config-file overlay layered over a baked-in YAML file (SPEC §4.7)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

import docuconf
from docuconf import DocuconfSettings, Overlay, Url

#: The file the app ships with, relative to its working directory.
BASE_FILE = "catalog.base.yaml"


class Search(BaseModel):
    url: Annotated[str, Url(schemes=("https",))] = Field(description="Search service base URL")
    timeout: timedelta = Field(timedelta(seconds=5), description="Search request timeout")


class CatalogSettings(DocuconfSettings):
    docuconf_service: ClassVar[str] = "catalog-api"
    docuconf_overlays: ClassVar[Sequence[Overlay]] = (
        Overlay(
            "platform",
            "/app/config/catalog.json",
            reload="watch",
            description="Platform overrides, layered over the baked-in settings",
        ),
    )
    model_config = SettingsConfigDict(env_prefix="CATALOG_", env_nested_delimiter="__")

    page_size: int = Field(20, ge=1, le=500, description="Items per page")
    cache_ttl: timedelta = Field(timedelta(minutes=1), description="How long listings stay cached")
    featured_categories: list[str] = Field(default_factory=list, description="Categories shown on the home page")
    log_level: Literal["debug", "info", "warn", "error"] = Field("info", description="Minimum log level")
    search: Search = Field(description="Search backend")
    db_password: SecretStr = Field(description="Catalog database password")

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
            YamlConfigSettingsSource(settings_cls, yaml_file=BASE_FILE),
        )


class YamlCatalogSettings(CatalogSettings):
    docuconf_overlays: ClassVar[Sequence[Overlay]] = (Overlay("platform", "/app/config/catalog.yaml"),)


class TomlCatalogSettings(CatalogSettings):
    docuconf_overlays: ClassVar[Sequence[Overlay]] = (Overlay("platform", "/app/config/catalog.toml"),)
