"""The orders service: GET /healthz, GET /config and POST /webhooks/payments, configured by docuconf."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Annotated, ClassVar, Literal

from pydantic import Field, SecretStr
from pydantic_settings import NoDecode

from docuconf import Csv, DocuconfSettings, Keys, KeySet, Url


# DocuconfSettings is pydantic-settings' BaseSettings, whose constructor runs docuconf's checks:
# Settings() and docuconf.load(Settings) are the same thing.
class Settings(DocuconfSettings):
    # metadata.name in the exported contract.
    docuconf_service: ClassVar[str] = "orders"

    port: int = Field(8080, ge=1, le=65535, description="HTTP listen port")
    log_level: Literal["debug", "info", "warn", "error"] = Field("info", description="Minimum log level")
    # SecretStr makes it a secret in the contract, and keeps it out of reprs and error messages.
    database_url: Annotated[SecretStr, Url(schemes=("postgres",))] = Field(
        max_length=2048, description="Postgres connection string for orders"
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
    """How long a request may take before the server gives up on it.

    Raise it when clients upload large order batches. Keep it below the load balancer's idle timeout, or the
    client sees a reset rather than a ``504``.

    The platform writes Go durations such as ``45s``; docuconf converts them to ISO 8601 for pydantic.
    """
    worker_count: int = Field(4, ge=1, le=64, description="Workers processing orders")
    # A key set: 1 or 2 keys (the defaults) of 32 to 256 characters, always secret. Each key prints as **********.
    webhook_keys: Annotated[KeySet, Keys(key_min_length=32, key_max_length=256)] | None = Field(
        None, description="Keys that verify the signature on incoming payment webhooks"
    )
    """A webhook is accepted when it is signed with any key in the set, so the key can be rotated without turning
    webhooks away. An empty or truncated key fails at boot. Without this variable, the service rejects every webhook.
    """


def verify(keys: KeySet | None, body: bytes, signature: str) -> bool:
    """Whether ``signature``, the hex HMAC-SHA256 of ``body``, was made with any of ``keys``.

    Accepting every key in the set is what lets a key be rotated: during the overlap the old and the new key both work.
    ``KeySet.verify`` tries every key, so the time taken does not say which one matched.
    """
    if keys is None:
        return False
    want = signature.lower().encode()
    return keys.verify(lambda key: hmac.compare_digest(hmac.new(key, body, hashlib.sha256).hexdigest().encode(), want))


def public_config(settings: Settings) -> dict[str, object]:
    """The typed values, by env name, with the secrets redacted, set or not."""
    values = settings.model_dump(mode="json")
    values["database_url"] = "***"
    values["webhook_keys"] = "***"
    return {name.upper(): value for name, value in values.items()}


MAX_BODY = 1 << 20


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

        def do_POST(self) -> None:
            if self.path != "/webhooks/payments":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self.send_error(413)
                return
            body = self.rfile.read(length)
            # Payment webhooks, signed with any key in WEBHOOK_KEYS (see Settings for how to rotate it).
            if not verify(settings.webhook_keys, body, self.headers.get("X-Signature", "")):
                self.send_error(401, "bad signature")
                return
            self.send_response(204)
            self.end_headers()

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
