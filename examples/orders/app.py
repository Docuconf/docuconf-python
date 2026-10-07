"""The orders service: GET /healthz and GET /config, configured by docuconf."""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Annotated, ClassVar, Literal

from pydantic import Field, SecretStr
from pydantic_settings import NoDecode

from docuconf import Csv, DocuconfSettings, Url


# DocuconfSettings is pydantic-settings' BaseSettings, whose constructor runs docuconf's checks:
# Settings() and docuconf.load(Settings) are the same thing.
class Settings(DocuconfSettings):
    # metadata.name in the exported contract.
    docuconf_service: ClassVar[str] = "orders"

    port: int = Field(8080, ge=1, le=65535, description="HTTP listen port")
    log_level: Literal["debug", "info", "warn", "error"] = Field("info", description="Minimum log level")
    # SecretStr makes it a secret in the contract, and keeps it out of reprs and error messages.
    database_url: Annotated[SecretStr, Url(schemes=("postgres",))] = Field(
        description="Postgres connection string for orders"
    )
    # NoDecode + Csv: read "a,b" rather than pydantic-settings' default JSON list.
    allowed_origins: Annotated[list[str], NoDecode, Csv()] = Field(
        ["http://localhost:3000"], min_length=1, description="CORS origins allowed to call the API"
    )
    # pydantic reads durations as ISO 8601 (PT45S); the contract says so, and the platform converts "45s".
    request_timeout: timedelta = Field(
        timedelta(seconds=30),
        ge=timedelta(seconds=1),
        le=timedelta(minutes=5),
        description="Timeout for a request to finish",
    )
    worker_count: int = Field(4, ge=1, le=64, description="Workers processing orders")


def public_config(settings: Settings) -> dict[str, object]:
    """The typed values, by env name, with the secret redacted."""
    values = settings.model_dump(mode="json")
    values["database_url"] = "***"
    return {name.upper(): value for name, value in values.items()}


def handler(settings: Settings) -> type[BaseHTTPRequestHandler]:
    config = json.dumps(public_config(settings)).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/healthz":
                self.reply(b"ok", "text/plain")
            elif self.path == "/config":
                self.reply(config, "application/json")
            else:
                self.send_error(404)

        def reply(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main() -> None:
    # Reads the environment and checks every rule. On failure it prints every problem at once, writes the
    # termination log and exits with status 1, without a traceback.
    settings = Settings.load_or_exit()
    logging.basicConfig(level=settings.log_level.upper())
    server = ThreadingHTTPServer(("", settings.port), handler(settings))
    logging.info("orders listening on :%d", settings.port)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
