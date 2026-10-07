"""The settings class of the README's Django recipe."""

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
