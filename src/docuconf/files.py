"""Boot-time checks for file inputs (SPEC §11.2 item 7)."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from pydantic import StringConstraints, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from .declaration import FileSpec
from .errors import DeclarationError, ErrorCode, Violation
from .markers import BinaryFile, CaBundleFile, ConfigFile, KeystoreFile, TextFile, TlsFile
from .values import CaBundle, Keystore, TlsKeyPair

_PEM_CERT = re.compile(rb"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.S)
FSGROUP_HINT = "; if the container runs as non-root, set the pod's securityContext.fsGroup"
MAX_SCHEMA_ERRORS = 10

EnvLookup = Callable[[str], "str | None"]


@dataclass
class FileResult:
    spec: FileSpec
    #: The value for the settings field, or None when absent or invalid.
    value: Any = None
    present: bool = False
    violations: list[Violation] = field(default_factory=list)
    #: The files read, for change detection with reload="watch".
    paths: list[Path] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def fail(self, code: ErrorCode, message: str) -> None:
        self.violations.append(Violation(self.spec.name, "file", code, message))


def resolve_path(spec: FileSpec, env: EnvLookup, environ: Mapping[str, str] | None = None) -> Path:
    """The declared path, or the pathEnv value, under DOCUCONF_FILE_ROOT if set."""
    environ = os.environ if environ is None else environ
    p = spec.marker.path
    if spec.marker.path_env:
        from_env = env(spec.marker.path_env)
        if from_env:
            p = from_env
    root = environ.get("DOCUCONF_FILE_ROOT")
    if root and os.path.isabs(p):
        return Path(root) / p.lstrip("/")
    return Path(p)


def _read(r: FileResult, path: Path, label: str, max_size: int | None, *, missing_ok: bool = False) -> bytes | None:
    r.paths.append(path)
    try:
        st = path.stat()
    except FileNotFoundError:
        if not missing_ok:
            r.fail("file_missing", f"{label} not found at {path}")
        return None
    except PermissionError:
        r.fail("file_unreadable", f"{label} at {path} cannot be accessed (permission denied){FSGROUP_HINT}")
        return None
    if path.is_dir():
        r.fail("file_unreadable", f"{label} at {path} is a directory, not a file")
        return None
    if max_size is not None and st.st_size > max_size:
        r.fail("file_too_large", f"{label} is {st.st_size} bytes, above maxSize {max_size}")
        return None
    try:
        return path.read_bytes()
    except PermissionError:
        r.fail("file_unreadable", f"{label} at {path} cannot be read (permission denied){FSGROUP_HINT}")
    except OSError as e:
        r.fail("file_unreadable", f"{label} at {path} cannot be read ({e.strerror})")
    return None


def load_file(spec: FileSpec, env: EnvLookup, *, now: datetime | None = None) -> FileResult:
    """Read and check one file input. Never raises for bad content."""
    r = FileResult(spec)
    path = resolve_path(spec, env)
    m = spec.marker
    now = now or datetime.now(timezone.utc)

    if isinstance(m, TlsFile):
        if not path.exists():
            r.paths.append(path / "tls.crt")
            if spec.required:
                r.fail("file_missing", f"TLS directory not found at {path}")
            return r
        r.present = True
        _load_tls(r, path, m, now)
        return r

    if not path.exists():
        r.paths.append(path)
        if spec.required:
            r.fail("file_missing", f"not found at {path}")
        return r
    r.present = True

    if (isinstance(m, BinaryFile) and spec.value_type is Path) or (
        isinstance(m, TextFile) and spec.value_type is Path and not (m.pattern or m.min_length or m.max_length)
    ):
        # The app reads the file itself; check it is there, readable and small enough.
        r.paths.append(path)
        try:
            size = path.stat().st_size
            with path.open("rb"):
                pass
        except PermissionError:
            r.fail("file_unreadable", f"{path} cannot be read (permission denied){FSGROUP_HINT}")
            return r
        except OSError as e:
            r.fail("file_unreadable", f"{path} cannot be read ({e.strerror})")
            return r
        if m.max_size is not None and size > m.max_size:
            r.fail("file_too_large", f"{size} bytes, above maxSize {m.max_size}")
        else:
            r.value = path
        return r

    data = _read(r, path, "file", m.max_size)
    if data is None:
        return r

    if isinstance(m, ConfigFile):
        _load_config(r, data, spec)
    elif isinstance(m, CaBundleFile):
        _load_ca_bundle(r, path, data, m)
    elif isinstance(m, KeystoreFile):
        _load_keystore(r, path, data, m, env)
    elif isinstance(m, TextFile):
        _load_text(r, path, data, m, spec)
    elif isinstance(m, BinaryFile):
        r.value = data
    return r


# -- config -----------------------------------------------------------------


def _parse(fmt: str, text: str) -> Any:
    if fmt == "json":
        return json.loads(text)
    if fmt == "yaml":
        try:
            import yaml
        except ImportError:
            raise DeclarationError(["YAML config files need PyYAML: pip install 'docuconf-pydantic[yaml]'"]) from None
        return yaml.safe_load(text)
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover
        import tomli as tomllib  # type: ignore[import-not-found,no-redef,unused-ignore]
    return tomllib.loads(text)


def _load_config(r: FileResult, data: bytes, spec: FileSpec) -> None:
    fmt = spec.contract_fields.get("format", "json")
    try:
        text = data.decode("utf-8-sig")  # tolerate a byte-order mark
    except UnicodeDecodeError:
        r.fail("file_malformed", "not valid UTF-8")
        return
    try:
        parsed = _parse(fmt, text)
    except DeclarationError:
        raise
    except Exception as e:
        detail = "" if spec.secret else f": {_one_line(str(e))}"
        r.fail("file_malformed", f"not valid {fmt.upper()}{detail}")
        return
    assert spec.adapter is not None
    try:
        r.value = spec.adapter.validate_python(parsed)
    except PydanticValidationError as e:
        errs = e.errors(include_input=False, include_url=False)
        for err in errs[:MAX_SCHEMA_ERRORS]:
            loc = ".".join(str(p) for p in err["loc"]) or "(root)"
            r.fail("schema_mismatch", f"{loc}: {err['msg']}")
        if len(errs) > MAX_SCHEMA_ERRORS:
            r.fail("schema_mismatch", f"... and {len(errs) - MAX_SCHEMA_ERRORS} more")


def _one_line(s: str) -> str:
    s = " ".join(s.split())
    return s if len(s) <= 200 else s[:197] + "..."


# -- text -------------------------------------------------------------------

_pattern_adapters: dict[str, TypeAdapter[str]] = {}


def matches(pattern: str, value: str) -> bool:
    """Match like pydantic's ``pattern`` (the Rust regex crate: RE2-like, unanchored)."""
    ta = _pattern_adapters.get(pattern)
    if ta is None:
        ta = TypeAdapter(Annotated[str, StringConstraints(pattern=pattern)])
        _pattern_adapters[pattern] = ta
    try:
        ta.validate_python(value)
        return True
    except PydanticValidationError:
        return False


def _load_text(r: FileResult, path: Path, data: bytes, m: TextFile, spec: FileSpec) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        r.fail("file_malformed", "not valid UTF-8 text")
        return
    n = len(text)
    if m.min_length is not None and n < m.min_length:
        r.fail("out_of_range", f"{n} characters, below minLength {m.min_length}")
    if m.max_length is not None and n > m.max_length:
        r.fail("out_of_range", f"{n} characters, above maxLength {m.max_length}")
    if m.pattern is not None and not matches(m.pattern, text):
        r.fail("pattern_mismatch", f"content does not match pattern {m.pattern!r}")
    if r.ok:
        r.value = path if spec.value_type is Path else text


# -- certificates -------------------------------------------------------------


def parse_certificates(r: FileResult, data: bytes, label: str, code: ErrorCode) -> list[x509.Certificate] | None:
    blocks = _PEM_CERT.findall(data)
    if not blocks:
        r.fail(code, f"{label} holds no PEM certificate")
        return None
    certs = []
    for i, block in enumerate(blocks):
        try:
            certs.append(x509.load_pem_x509_certificate(block))
        except ValueError:
            r.fail(code, f"{label}: certificate {i + 1} cannot be parsed")
            return None
    return certs


def key_algorithm(key: Any) -> str:
    if isinstance(key, rsa.RSAPublicKey):
        return "RSA"
    if isinstance(key, ec.EllipticCurvePublicKey):
        return "ECDSA"
    if isinstance(key, ed25519.Ed25519PublicKey):
        return "Ed25519"
    return type(key).__name__


def _spki(key: Any) -> bytes:
    der: bytes = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return der


def covers(cert: x509.Certificate, name: str) -> bool:
    """Whether the certificate's SAN DNS names cover ``name``; a wildcard covers one label."""
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return False
    want = name.lower().rstrip(".")
    for dns in san.get_values_for_type(x509.DNSName):
        d = dns.lower().rstrip(".")
        if d == want:
            return True
        if d.startswith("*.") and "." in want:
            label, rest = want.split(".", 1)
            if label and rest == d[2:]:
                return True
    return False


def _fmt(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def verify_chain(chain: list[x509.Certificate], cas: list[x509.Certificate], now: datetime) -> str | None:
    """Verify ``chain`` (leaf first) up to one of ``cas``. Returns an error message or None.

    Uses ``cryptography.x509.verification`` (path building, signatures,
    validity, CA basic constraints) with permissive extension policies, since
    private CAs rarely follow the Web PKI profile. With cryptography older than
    45, falls back to checking that the top of the chain is, or is directly
    issued by, a certificate in ``ca.crt``.
    """
    try:
        from cryptography.x509.verification import (
            Criticality,
            ExtensionPolicy,
            PolicyBuilder,
            Store,
            VerificationError,
        )
    except ImportError:  # pragma: no cover - cryptography < 45
        return _verify_chain_fallback(chain, cas)
    ca_policy = ExtensionPolicy.permit_all().require_present(x509.BasicConstraints, Criticality.AGNOSTIC, None)
    builder = (
        PolicyBuilder()
        .store(Store(cas))
        .time(now)
        .extension_policies(ca_policy=ca_policy, ee_policy=ExtensionPolicy.permit_all())
    )
    try:
        builder.build_client_verifier().verify(chain[0], chain[1:])
    except (VerificationError, ValueError) as e:
        return f"tls.crt does not chain to a certificate in ca.crt ({e})"
    return None


def _verify_chain_fallback(chain: list[x509.Certificate], cas: list[x509.Certificate]) -> str | None:
    top = chain[-1]
    for ca in cas:
        if ca == top:
            return None
        try:
            top.verify_directly_issued_by(ca)
            return None
        except (ValueError, TypeError, Exception):
            continue
    return "tls.crt does not chain to a certificate in ca.crt"


def check_tls(
    r: FileResult,
    cert_pem: bytes,
    key_pem: bytes,
    ca_pem: bytes | None,
    m: TlsFile,
    min_remaining_ns: int | None,
    now: datetime,
) -> tuple[list[x509.Certificate], Any] | None:
    before = len(r.violations)
    chain = parse_certificates(r, cert_pem, "tls.crt", "certificate_invalid")
    key = None
    try:
        key = serialization.load_pem_private_key(key_pem, password=None)
    except (ValueError, TypeError):
        # The parser's message could quote key material; keep it generic.
        r.fail("certificate_invalid", "tls.key is not a readable, unencrypted PEM private key")
    except Exception:
        r.fail("certificate_invalid", "tls.key is not a readable PEM private key")
    if chain is None:
        return None
    leaf = chain[0]
    if key is not None and _spki(key.public_key()) != _spki(leaf.public_key()):
        r.fail("key_mismatch", "tls.key does not match the certificate in tls.crt")

    not_before, not_after = leaf.not_valid_before_utc, leaf.not_valid_after_utc
    if now < not_before:
        r.fail("certificate_invalid", f"certificate is not valid until {_fmt(not_before)}")
    elif now > not_after:
        r.fail("certificate_invalid", f"certificate expired at {_fmt(not_after)}")
    elif min_remaining_ns is not None and not_after - now < timedelta(microseconds=min_remaining_ns // 1000):
        r.fail(
            "certificate_expiring",
            f"certificate expires at {_fmt(not_after)}, less than {m.min_remaining} from now",
        )

    for name in m.dns_names:
        if not covers(leaf, name):
            r.fail("certificate_name_mismatch", f"certificate does not cover {name}")

    if m.key_algorithms:
        alg = key_algorithm(leaf.public_key())
        if alg not in m.key_algorithms:
            r.fail("certificate_invalid", f"key algorithm {alg} is not one of {', '.join(m.key_algorithms)}")

    # tls.crt must be ordered leaf first, each certificate issued by the next.
    for i in range(len(chain) - 1):
        try:
            chain[i].verify_directly_issued_by(chain[i + 1])
        except Exception:
            r.fail(
                "certificate_invalid",
                f"tls.crt: certificate {i + 1} is not issued by certificate {i + 2}; order the chain leaf first",
            )
            break

    if m.require_ca:
        if ca_pem is None:
            r.fail("file_missing", "ca.crt is required (requireCA) but missing")
        else:
            cas = parse_certificates(r, ca_pem, "ca.crt", "certificate_invalid")
            if cas:
                err = verify_chain(chain, cas, now)
                if err:
                    r.fail("certificate_invalid", err)
    if len(r.violations) > before or key is None:
        return None
    return chain, key


def _load_tls(r: FileResult, directory: Path, m: TlsFile, now: datetime) -> None:
    cert = _read(r, directory / "tls.crt", "tls.crt", m.max_size)
    key = _read(r, directory / "tls.key", "tls.key", m.max_size)
    ca = _read(r, directory / "ca.crt", "ca.crt", m.max_size, missing_ok=not m.require_ca)
    if m.require_ca and ca is None:
        return
    if cert is None or key is None:
        return
    checked = check_tls(r, cert, key, ca, m, r.spec.min_remaining_ns, now)
    if checked is not None:
        chain, private_key = checked
        r.value = TlsKeyPair(directory, cert, key, ca, chain, private_key)


def _load_ca_bundle(r: FileResult, path: Path, data: bytes, m: CaBundleFile) -> None:
    certs = parse_certificates(r, data, "bundle", "file_malformed")
    if certs is None:
        return
    if len(certs) < m.min_certificates:
        r.fail("file_malformed", f"holds {len(certs)} certificate(s), needs at least {m.min_certificates}")
        return
    r.value = CaBundle(path, data, certs)


def _load_keystore(r: FileResult, path: Path, data: bytes, m: KeystoreFile, env: EnvLookup) -> None:
    password = env(m.password_var) if m.password_var else None
    try:
        key, cert, extra = pkcs12.load_key_and_certificates(data, password.encode("utf-8") if password else None)
    except Exception:
        # Never echo the password or the parser's detail.
        hint = f" with the password in {m.password_var}" if m.password_var else " without a password"
        r.fail("keystore_unreadable", f"cannot open the PKCS#12 keystore{hint}")
        return
    r.value = Keystore(path, key, cert, list(extra))
