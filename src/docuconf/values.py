"""Typed values docuconf loads from file inputs."""

from __future__ import annotations

import logging
import ssl
import threading
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import core_schema

if TYPE_CHECKING:  # cryptography is the optional ``tls`` extra
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes

log = logging.getLogger("docuconf")


def call_hooks(name: str, hooks: list[Callable[..., None]], *args: Any) -> None:
    """Call each hook; one that raises is logged by input name and error type only, and the rest still run."""
    for hook in list(hooks):
        try:
            hook(*args)
        except Exception as e:
            # Never the message or traceback: either could carry the new value.
            log.error("docuconf: an on-change hook for %s raised %s", name, type(e).__name__)


class _FileValue:
    """Base for values that pydantic accepts as-is (an ``isinstance`` check)."""

    _kind = "file"

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        return core_schema.is_instance_schema(cls)

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return {"type": "object", "description": f"{cls._kind}, loaded by docuconf"}

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._listeners: list[Callable[[Any], None]] = []

    def on_change(self, listener: Callable[[Any], None]) -> Callable[[], None]:
        """Call ``listener(self)`` after each accepted reload (``reload="watch"``), on the watcher thread.

        Never called for a rejected change. Returns a function that removes the listener.
        """
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _notify(self, name: str) -> None:
        call_hooks(name, self._listeners, self)


class TlsKeyPair(_FileValue):
    """A checked TLS key pair from a ``kubernetes.io/tls`` directory.

    With ``reload="watch"`` the object is updated in place when the files
    change, and every ``ssl.SSLContext`` made by :meth:`server_context` or
    :meth:`client_context` is reloaded for new connections.
    """

    _kind = "TLS key pair"

    def __init__(
        self,
        directory: Path,
        cert_pem: bytes,
        key_pem: bytes,
        ca_pem: bytes | None,
        chain: list[x509.Certificate],
        private_key: PrivateKeyTypes,
    ) -> None:
        super().__init__()
        self.directory = directory
        self.cert_pem = cert_pem
        self.key_pem = key_pem
        self.ca_pem = ca_pem
        self.chain = chain
        self.private_key = private_key
        self._contexts: weakref.WeakSet[ssl.SSLContext] = weakref.WeakSet()

    @property
    def certificate(self) -> x509.Certificate:
        """The leaf certificate."""
        return self.chain[0]

    @property
    def cert_file(self) -> Path:
        return self.directory / "tls.crt"

    @property
    def key_file(self) -> Path:
        return self.directory / "tls.key"

    @property
    def ca_file(self) -> Path | None:
        return self.directory / "ca.crt" if self.ca_pem is not None else None

    def _load_into(self, ctx: ssl.SSLContext) -> None:
        ctx.load_cert_chain(str(self.cert_file), str(self.key_file))
        if self.ca_pem is not None:
            ctx.load_verify_locations(cadata=self.ca_pem.decode("ascii", "replace"))

    def server_context(self) -> ssl.SSLContext:
        """An ``SSLContext`` for serving with this key pair (trusting ``ca.crt`` for client certs, if present)."""
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self._load_into(ctx)
        self._contexts.add(ctx)
        return ctx

    def client_context(self) -> ssl.SSLContext:
        """An ``SSLContext`` presenting this key pair as a client certificate."""
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        self._load_into(ctx)
        self._contexts.add(ctx)
        return ctx

    def _replace(self, new: TlsKeyPair) -> None:
        with self._lock:
            self.cert_pem, self.key_pem, self.ca_pem = new.cert_pem, new.key_pem, new.ca_pem
            self.chain, self.private_key = new.chain, new.private_key
            for ctx in list(self._contexts):
                try:
                    self._load_into(ctx)
                except (OSError, ssl.SSLError):
                    log.exception("docuconf: could not reload an SSLContext")

    def __repr__(self) -> str:
        subject = self.certificate.subject.rfc4514_string()
        return f"TlsKeyPair(directory={str(self.directory)!r}, subject={subject!r}, key='**********')"


class CaBundle(_FileValue):
    """A checked PEM bundle of CA certificates."""

    _kind = "CA bundle"

    def __init__(self, path: Path, pem: bytes, certificates: list[x509.Certificate]) -> None:
        super().__init__()
        self.path = path
        self.pem = pem
        self.certificates = certificates

    def client_context(self) -> ssl.SSLContext:
        """An ``SSLContext`` that trusts (only) this bundle."""
        ctx = ssl.create_default_context(cadata=self.pem.decode("ascii", "replace"))
        return ctx

    def _replace(self, new: CaBundle) -> None:
        with self._lock:
            self.pem, self.certificates = new.pem, new.certificates

    def __repr__(self) -> str:
        return f"CaBundle(path={str(self.path)!r}, certificates={len(self.certificates)})"


class Keystore(_FileValue):
    """An opened PKCS#12 keystore."""

    _kind = "PKCS#12 keystore"

    def __init__(
        self,
        path: Path,
        private_key: PrivateKeyTypes | None,
        certificate: x509.Certificate | None,
        additional_certificates: list[x509.Certificate],
    ) -> None:
        super().__init__()
        self.path = path
        self.private_key = private_key
        self.certificate = certificate
        self.additional_certificates = additional_certificates

    def _replace(self, new: Keystore) -> None:
        with self._lock:
            self.private_key = new.private_key
            self.certificate = new.certificate
            self.additional_certificates = new.additional_certificates

    def __repr__(self) -> str:
        subject = self.certificate.subject.rfc4514_string() if self.certificate else None
        return f"Keystore(path={str(self.path)!r}, subject={subject!r}, key='**********')"
