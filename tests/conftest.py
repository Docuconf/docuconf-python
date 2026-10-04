from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from docuconf.declaration import declaration
from tests.certs import make_ca, make_leaf, write_p12, write_tls
from tests.fixtures.sample_settings import GatewaySettings

LICENSE = "ABCDE-12345-FGHIJ-67890\n"
ROUTES_YAML = """\
routes:
  - match: /api
    upstream: https://api.internal
    timeout: 5s
"""

VALID_ENV = {
    "ALLOWED_ORIGINS": "https://a.example.com,https://b.example.com",
    "DATABASE_URL": "postgres://app:s3cr3t-pw@db:5432/app",
    "KEYSTORE_PASSWORD": "changeit",
    "PUBLIC_URL": "https://gateway.example.com",
    "REGION": "eu-west-1",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Remove every variable the sample declares, so the host environment cannot leak in."""
    decl = declaration(GatewaySettings)
    names = {v.name for v in decl.vars} | {f.marker.path_env for f in decl.files if f.marker.path_env}
    for key in list(os.environ):
        if key.upper() in names or key.startswith("DOCUCONF_") or key.upper().startswith(("APP_", "SVC_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("DOCUCONF_TERMINATION_LOG", str(tmp_path / "termination-log"))
    yield


@pytest.fixture
def gateway_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A DOCUCONF_FILE_ROOT with valid files for every input of the sample, and a valid environment."""
    root = tmp_path / "root"
    ca = make_ca()
    leaf = make_leaf(["gateway.internal", "api.example.com"], issuer=ca)
    write_tls(root / "etc/gateway/tls", leaf, ca=ca)
    (root / "etc/gateway/ca").mkdir(parents=True)
    (root / "etc/gateway/ca/bundle.pem").write_bytes(ca.cert_pem)
    write_p12(root / "etc/gateway/partner/keystore.p12", make_leaf(["partner.client"]), b"changeit")
    (root / "etc/gateway/license").mkdir(parents=True)
    (root / "etc/gateway/license/license.key").write_text(LICENSE)
    (root / "etc/gateway/routes").mkdir(parents=True)
    (root / "etc/gateway/routes/routes.yaml").write_text(ROUTES_YAML)
    (root / "data/geoip").mkdir(parents=True)
    (root / "data/geoip/GeoLite2-City.mmdb").write_bytes(b"\x00geoip")
    monkeypatch.setenv("DOCUCONF_FILE_ROOT", str(root))
    for k, v in VALID_ENV.items():
        monkeypatch.setenv(k, v)
    return root


def find_cue() -> str | None:
    for c in (os.environ.get("CUE"), os.path.expanduser("~/go/bin/cue"), shutil.which("cue")):
        if c and os.path.exists(c):
            try:
                subprocess.run([c, "version"], check=True, capture_output=True)
                return c
            except (OSError, subprocess.CalledProcessError):
                continue
    return None
