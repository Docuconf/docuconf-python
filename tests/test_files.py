"""Boot checks of file inputs with real files (SPEC §11.2 item 7)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

import pytest
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

import docuconf
from docuconf import CaBundle, ConfigFile, ConfigValidationError, TlsFile, TlsKeyPair
from tests.certs import make_ca, make_leaf, new_key, write_p12, write_tls
from tests.fixtures.sample_settings import GatewaySettings

TLS_DIR = "etc/gateway/tls"


def load_error(cls: type[BaseSettings] = GatewaySettings) -> ConfigValidationError:
    with pytest.raises(ConfigValidationError) as info:
        docuconf.load(cls, watch=False)
    return info.value


def file_codes(err: ConfigValidationError) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for v in err.violations:
        out.setdefault(v.input, []).append(v.code)
    return out


def test_valid_files(gateway_root: Path) -> None:
    s = docuconf.load(GatewaySettings, watch=False)
    assert isinstance(s.serving_tls, TlsKeyPair)
    assert s.serving_tls.certificate.subject.rfc4514_string() == "CN=gateway.internal"
    assert isinstance(s.upstream_ca, CaBundle) and len(s.upstream_ca.certificates) == 1
    assert s.partner_keystore is not None and s.partner_keystore.certificate is not None
    assert s.routes.routes[0].upstream == "https://api.internal"
    assert s.license.startswith("ABCDE")
    assert s.geoip == gateway_root / "data/geoip/GeoLite2-City.mmdb"
    assert "PRIVATE KEY" not in repr(s)
    ctx = s.serving_tls.server_context()
    assert ctx is not None


def test_optional_files_may_be_absent(gateway_root: Path) -> None:
    (gateway_root / "etc/gateway/ca/bundle.pem").unlink()
    (gateway_root / "etc/gateway/partner/keystore.p12").unlink()
    (gateway_root / "data/geoip/GeoLite2-City.mmdb").unlink()
    s = docuconf.load(GatewaySettings, watch=False)
    assert (s.upstream_ca, s.partner_keystore, s.geoip) == (None, None, None)


def test_missing_required_files(gateway_root: Path) -> None:
    for f in ("tls.crt", "tls.key", "ca.crt"):
        (gateway_root / TLS_DIR / f).unlink()
    (gateway_root / "etc/gateway/routes/routes.yaml").unlink()
    (gateway_root / "etc/gateway/license/license.key").unlink()
    err = load_error()
    assert file_codes(err) == {
        "license": ["file_missing"],
        "routes": ["file_missing"],
        "serving-tls": ["file_missing", "file_missing", "file_missing"],
    }


def test_expiring_certificate(gateway_root: Path) -> None:
    ca = make_ca()
    write_tls(gateway_root / TLS_DIR, make_leaf(["gateway.internal", "api.example.com"], issuer=ca, days=10), ca=ca)
    err = load_error()
    assert file_codes(err) == {"serving-tls": ["certificate_expiring"]}
    assert "720h" in str(err)


def test_expired_and_not_yet_valid(gateway_root: Path) -> None:
    ca = make_ca()
    now = datetime.now(timezone.utc)
    names = ["gateway.internal", "api.example.com"]
    write_tls(
        gateway_root / TLS_DIR,
        make_leaf(names, issuer=ca, not_before=now - timedelta(days=30), not_after=now - timedelta(days=1)),
        ca=ca,
    )
    err = load_error()
    # With cryptography >= 45 the chain check also reports the expired leaf.
    assert set(file_codes(err)["serving-tls"]) == {"certificate_invalid"}
    assert "certificate expired at" in str(err)
    write_tls(
        gateway_root / TLS_DIR,
        make_leaf(names, issuer=ca, not_before=now + timedelta(days=1), not_after=now + timedelta(days=90)),
        ca=ca,
    )
    assert "not valid until" in str(load_error())


def test_dns_mismatch_and_wildcards(gateway_root: Path) -> None:
    ca = make_ca()
    write_tls(gateway_root / TLS_DIR, make_leaf(["gateway.internal", "*.other.com"], issuer=ca), ca=ca)
    err = load_error()
    assert file_codes(err) == {"serving-tls": ["certificate_name_mismatch"]}
    assert "api.example.com" in str(err)
    # A wildcard covers exactly one label.
    write_tls(gateway_root / TLS_DIR, make_leaf(["gateway.internal", "*.example.com"], issuer=ca), ca=ca)
    docuconf.load(GatewaySettings, watch=False)


def test_wildcard_does_not_cover_two_labels() -> None:
    from docuconf.files import covers

    cert = make_leaf(["*.example.com"]).cert
    assert covers(cert, "api.example.com")
    assert covers(cert, "API.Example.com")
    assert not covers(cert, "a.b.example.com")
    assert not covers(cert, "example.com")


def test_key_mismatch(gateway_root: Path) -> None:
    ca = make_ca()
    write_tls(
        gateway_root / TLS_DIR, make_leaf(["gateway.internal", "api.example.com"], issuer=ca), ca=ca, key=new_key()
    )
    assert file_codes(load_error()) == {"serving-tls": ["key_mismatch"]}


def test_key_algorithm(gateway_root: Path) -> None:
    ca = make_ca()
    write_tls(
        gateway_root / TLS_DIR, make_leaf(["gateway.internal", "api.example.com"], issuer=ca, alg="Ed25519"), ca=ca
    )
    err = load_error()
    assert file_codes(err) == {"serving-tls": ["certificate_invalid"]}
    assert "Ed25519" in str(err)


def test_rsa_key_accepted(gateway_root: Path) -> None:
    ca = make_ca()
    write_tls(gateway_root / TLS_DIR, make_leaf(["gateway.internal", "api.example.com"], issuer=ca, alg="RSA"), ca=ca)
    docuconf.load(GatewaySettings, watch=False)


def test_chain_to_wrong_ca(gateway_root: Path) -> None:
    other = make_ca("Other CA")
    leaf = make_leaf(["gateway.internal", "api.example.com"], issuer=make_ca())
    write_tls(gateway_root / TLS_DIR, leaf, ca=other)
    err = load_error()
    assert file_codes(err) == {"serving-tls": ["certificate_invalid"]}
    assert "does not chain" in str(err)


def test_chain_through_intermediate(gateway_root: Path) -> None:
    root = make_ca("Root")
    inter = make_ca("Intermediate", parent=root)
    leaf = make_leaf(["gateway.internal", "api.example.com"], issuer=inter)
    write_tls(gateway_root / TLS_DIR, leaf, chain=[inter], ca=root)
    docuconf.load(GatewaySettings, watch=False)
    # Wrong order: intermediate first.
    (gateway_root / TLS_DIR / "tls.crt").write_bytes(inter.cert_pem + leaf.cert_pem)
    assert "certificate_invalid" in file_codes(load_error())["serving-tls"]


def test_garbage_certificate_and_key(gateway_root: Path) -> None:
    (gateway_root / TLS_DIR / "tls.crt").write_text(
        "-----BEGIN CERTIFICATE-----\nbm9wZQ==\n-----END CERTIFICATE-----\n"
    )
    (gateway_root / TLS_DIR / "tls.key").write_text("not a key")
    assert file_codes(load_error()) == {"serving-tls": ["certificate_invalid", "certificate_invalid"]}


def test_malformed_config(gateway_root: Path) -> None:
    (gateway_root / "etc/gateway/routes/routes.yaml").write_text("routes: [unclosed\n")
    assert file_codes(load_error()) == {"routes": ["file_malformed"]}


def test_config_schema_mismatch(gateway_root: Path) -> None:
    (gateway_root / "etc/gateway/routes/routes.yaml").write_text("routes:\n  - match: api\n    upstream: ftp://x\n")
    err = load_error()
    assert file_codes(err) == {"routes": ["schema_mismatch", "schema_mismatch"]}
    assert "routes.0.match" in str(err)


def test_config_file_too_large(gateway_root: Path) -> None:
    (gateway_root / "etc/gateway/routes/routes.yaml").write_text("#" * 70000)
    assert file_codes(load_error()) == {"routes": ["file_too_large"]}


def test_path_env_and_file_root(gateway_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    alt = gateway_root / "srv/routes.yaml"
    alt.parent.mkdir(parents=True)
    alt.write_text("routes:\n  - match: /alt\n    upstream: http://alt\n")
    monkeypatch.setenv("ROUTES_FILE", "/srv/routes.yaml")  # absolute: gets DOCUCONF_FILE_ROOT too
    s = docuconf.load(GatewaySettings, watch=False)
    assert s.routes.routes[0].match == "/alt"


def test_ca_bundle_needs_certificates(gateway_root: Path) -> None:
    (gateway_root / "etc/gateway/ca/bundle.pem").write_text("nothing here\n")
    assert file_codes(load_error()) == {"upstream-ca": ["file_malformed"]}


def test_keystore_wrong_password(gateway_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYSTORE_PASSWORD", "wrong-password-xyz")
    err = load_error()
    assert file_codes(err) == {"partner-keystore": ["keystore_unreadable"]}
    assert "wrong-password-xyz" not in str(err)


def test_keystore_garbage(gateway_root: Path) -> None:
    (gateway_root / "etc/gateway/partner/keystore.p12").write_bytes(b"\x30\x00garbage")
    assert file_codes(load_error()) == {"partner-keystore": ["keystore_unreadable"]}


def test_text_constraints(gateway_root: Path) -> None:
    lic = gateway_root / "etc/gateway/license/license.key"
    lic.write_text("ABCDE-12345-FGHIJ-67890")  # no trailing newline: still matches
    docuconf.load(GatewaySettings, watch=False)
    lic.write_text("ABCDE-12345-FGHIJ-67890\n\n")  # $ is end of text, as in RE2
    assert file_codes(load_error()) == {"license": ["pattern_mismatch"]}


def test_binary_too_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from docuconf import BinaryFile

    class S(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="APP_")
        blob: Annotated[bytes, BinaryFile(path="/data/blob/blob.bin", max_size=4)] = Field(description="Opaque blob")

    monkeypatch.setenv("DOCUCONF_FILE_ROOT", str(tmp_path))
    (tmp_path / "data/blob").mkdir(parents=True)
    (tmp_path / "data/blob/blob.bin").write_bytes(b"12345")
    assert file_codes(load_error(S)) == {"blob": ["file_too_large"]}
    (tmp_path / "data/blob/blob.bin").write_bytes(b"1234")
    assert docuconf.load(S, watch=False).blob == b"1234"


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root can read any file")
def test_unreadable_file(gateway_root: Path) -> None:
    lic = gateway_root / "etc/gateway/license/license.key"
    lic.chmod(0)
    err = load_error()
    assert file_codes(err) == {"license": ["file_unreadable"]}
    assert "fsGroup" in str(err)


def test_toml_and_json_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Limits(BaseModel):
        rate: int = Field(ge=1)

    class S(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="APP_")
        a: Annotated[Limits, ConfigFile(path="/etc/app/a/limits.toml")] = Field(description="Limits as TOML")
        b: Annotated[Limits, ConfigFile(path="/etc/app/b/limits.json")] = Field(description="Limits as JSON")

    monkeypatch.setenv("DOCUCONF_FILE_ROOT", str(tmp_path))
    (tmp_path / "etc/app/a").mkdir(parents=True)
    (tmp_path / "etc/app/b").mkdir(parents=True)
    (tmp_path / "etc/app/a/limits.toml").write_text("rate = 5\n")
    (tmp_path / "etc/app/b/limits.json").write_bytes(b"\xef\xbb\xbf" + b'{"rate": 7}')  # with a BOM
    s = docuconf.load(S, watch=False)
    assert (s.a.rate, s.b.rate) == (5, 7)
    (tmp_path / "etc/app/a/limits.toml").write_text("rate = \n")
    (tmp_path / "etc/app/b/limits.json").write_text('{"rate": 0}')
    assert file_codes(load_error(S)) == {"a": ["file_malformed"], "b": ["schema_mismatch"]}


def test_watch_reloads_tls_and_config(gateway_root: Path) -> None:
    s = docuconf.load(GatewaySettings, watch_interval=3600)
    w = docuconf.get_watcher(s)
    assert w is not None and w.inputs == ["routes", "serving-tls"]
    pair = s.serving_tls
    seen: list[str] = []
    w.on_reload(lambda name, value: seen.append(name))
    changed: list[TlsKeyPair] = []
    pair.on_change(changed.append)
    ctx = pair.server_context()
    try:
        assert w.check_now() == []
        # Kubernetes-style atomic update: write new files, then swap them in.
        ca = make_ca()
        new_leaf = make_leaf(["gateway.internal", "api.example.com"], issuer=ca)
        staging = gateway_root / "staging"
        write_tls(staging, new_leaf, ca=ca)
        for f in ("tls.crt", "tls.key", "ca.crt"):
            os.replace(staging / f, gateway_root / TLS_DIR / f)
        (gateway_root / "etc/gateway/routes/routes.yaml").write_text(
            "routes:\n  - match: /v2\n    upstream: https://v2.internal\n"
        )
        assert sorted(w.check_now()) == ["routes", "serving-tls"]
        assert s.serving_tls is pair and pair.certificate == new_leaf.cert
        assert changed == [pair]
        assert s.routes.routes[0].match == "/v2"
        assert sorted(seen) == ["routes", "serving-tls"]
        assert ctx is not None

        # A bad update is rejected and the previous value kept.
        (gateway_root / "etc/gateway/routes/routes.yaml").write_text("routes: []\n")
        assert w.check_now() == []
        assert s.routes.routes[0].match == "/v2"
    finally:
        w.stop()
    assert docuconf.get_watcher(s) is None


def test_tls_directory_not_found_when_optional(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class S(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="APP_")
        tls: Annotated[TlsKeyPair | None, TlsFile(path="/etc/app/tls")] = Field(None, description="Optional TLS")

    monkeypatch.setenv("DOCUCONF_FILE_ROOT", str(tmp_path))
    assert docuconf.load(S, watch=False).tls is None


def test_p12_without_password(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from docuconf import Keystore, KeystoreFile

    class S(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="APP_")
        ks: Annotated[Keystore, KeystoreFile(path="/etc/app/ks/ks.p12")] = Field(description="Client keystore")

    monkeypatch.setenv("DOCUCONF_FILE_ROOT", str(tmp_path))
    write_p12(tmp_path / "etc/app/ks/ks.p12", make_leaf(["c"]), None)
    assert docuconf.load(S, watch=False).ks.private_key is not None
