"""Self-signed certificates for tests, made with cryptography."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


@dataclass
class Issued:
    cert: x509.Certificate
    key: Any

    @property
    def cert_pem(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.PEM)

    @property
    def key_pem(self) -> bytes:
        return self.key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )


def new_key(alg: str = "ECDSA") -> Any:
    if alg == "RSA":
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if alg == "Ed25519":
        return ed25519.Ed25519PrivateKey.generate()
    return ec.generate_private_key(ec.SECP256R1())


def _sign_hash(key: Any) -> Any:
    return None if isinstance(key, ed25519.Ed25519PrivateKey) else hashes.SHA256()


def make_ca(name: str = "Test CA", *, days: int = 3650, parent: Issued | None = None) -> Issued:
    key = new_key("ECDSA")
    now = datetime.now(timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    issuer_key = parent.key if parent else key
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(parent.cert.subject if parent else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    if parent:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(parent.key.public_key()), critical=False
        )
    return Issued(builder.sign(issuer_key, _sign_hash(issuer_key)), key)


def make_leaf(
    dns_names: list[str],
    *,
    issuer: Issued | None = None,
    alg: str = "ECDSA",
    not_before: datetime | None = None,
    not_after: datetime | None = None,
    days: int = 90,
) -> Issued:
    key = new_key(alg)
    now = datetime.now(timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_names[0] if dns_names else "leaf")])
    signer = issuer.key if issuer else key
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer.cert.subject if issuer else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or now - timedelta(hours=1))
        .not_valid_after(not_after or now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
        )
    )
    if dns_names:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in dns_names]), critical=False
        )
    if issuer:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer.key.public_key()), critical=False
        )
    return Issued(builder.sign(signer, _sign_hash(signer)), key)


def write_tls(
    directory: Path, leaf: Issued, *, chain: list[Issued] = (), ca: Issued | None = None, key: Any = None
) -> None:  # type: ignore[assignment]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "tls.crt").write_bytes(leaf.cert_pem + b"".join(c.cert_pem for c in chain))
    key_pem = leaf.key_pem if key is None else Issued(leaf.cert, key).key_pem
    (directory / "tls.key").write_bytes(key_pem)
    if ca is not None:
        (directory / "ca.crt").write_bytes(ca.cert_pem)


def write_p12(path: Path, leaf: Issued, password: bytes | None) -> None:
    enc = serialization.BestAvailableEncryption(password) if password else serialization.NoEncryption()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pkcs12.serialize_key_and_certificates(b"partner", leaf.key, leaf.cert, None, enc))
