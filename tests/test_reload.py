"""``reload="watch"``: on-change hooks, reload status and the keystore password (SPEC §4.6.2, §11.2 item 8)."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import pytest
from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

import docuconf
from docuconf import DocuconfSettings, Keystore, KeystoreFile, ReloadStatus, TextFile, Watcher
from tests.certs import make_leaf, write_p12

SECRET = "token-0123456789-very-secret"


class Svc(DocuconfSettings):
    model_config = SettingsConfigDict(env_prefix="SVC_")

    token: Annotated[str, TextFile(path="/etc/svc/token", pattern=r"token-\S+", reload="watch"), docuconf.Secret()] = (
        Field(description="Token the service presents")
    )
    keystore_password: SecretStr = Field(description="Password for the client keystore")
    keystore: Annotated[
        Keystore, KeystoreFile(path="/etc/svc/ks/client.p12", password_var="SVC_KEYSTORE_PASSWORD", reload="watch")
    ] = Field(description="Client keystore")


@pytest.fixture
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "root"
    (root / "etc/svc").mkdir(parents=True)
    (root / "etc/svc/token").write_text(SECRET)
    write_p12(root / "etc/svc/ks/client.p12", make_leaf(["first.client"]), b"changeit")
    monkeypatch.setenv("DOCUCONF_FILE_ROOT", str(root))
    monkeypatch.setenv("SVC_KEYSTORE_PASSWORD", "changeit")
    return root


def swap(path: Path, content: str) -> None:
    """Replace a file the way Kubernetes does: a new inode behind the same path."""
    tmp = path.with_name(path.name + ".new")
    tmp.write_text(content)
    os.replace(tmp, path)


def watcher(s: Any) -> Watcher:
    w = docuconf.get_watcher(s)
    assert w is not None
    return w


def test_hooks_fire_on_accepted_changes_only(svc: Path) -> None:
    s = docuconf.load(Svc, watch_interval=3600)
    w = watcher(s)
    seen: list[str] = []
    all_inputs: list[str] = []
    remove = w.on_change("token", seen.append)
    w.on_change("token", lambda v: seen.append(f"second:{v}"))
    w.on_reload(lambda name, value: all_inputs.append(name))
    try:
        swap(svc / "etc/svc/token", "token-new-value")
        assert w.check_now() == ["token"]
        assert seen == ["token-new-value", "second:token-new-value"]
        assert all_inputs == ["token"]
        assert s.token == "token-new-value"

        # Rejected: no hook, the previous value is kept.
        swap(svc / "etc/svc/token", "not a token")
        assert w.check_now() == []
        assert len(seen) == 2 and all_inputs == ["token"]
        assert s.token == "token-new-value"

        remove()
        remove()  # removing twice is harmless
        swap(svc / "etc/svc/token", "token-third")
        assert w.check_now() == ["token"]
        assert seen[2:] == ["second:token-third"]
    finally:
        w.stop()


def test_hooks_fire_without_a_read(svc: Path) -> None:
    """The watcher thread polls on its own, so a hook runs although nothing reads the value."""
    s = docuconf.load(Svc, watch_interval=0.05)
    w = watcher(s)
    fired = threading.Event()
    w.on_change("token", lambda value: fired.set())
    try:
        swap(svc / "etc/svc/token", "token-from-the-thread")
        assert fired.wait(10)
        assert w.status("token").generation == 2
    finally:
        w.stop()


def test_hook_that_raises_does_not_stop_the_others(svc: Path, caplog: pytest.LogCaptureFixture) -> None:
    s = docuconf.load(Svc, watch_interval=3600)
    w = watcher(s)
    seen: list[str] = []

    def boom(value: str) -> None:
        raise ValueError(f"cannot use {value}")

    w.on_change("token", boom)
    w.on_change("token", seen.append)
    try:
        swap(svc / "etc/svc/token", "token-rotated-secret")
        with caplog.at_level(logging.INFO, logger="docuconf"):
            assert w.check_now() == ["token"]
        assert seen == ["token-rotated-secret"] and s.token == "token-rotated-secret"
        assert w.status("token").generation == 2
        assert "an on-change hook for token raised ValueError" in caplog.text
        assert "token-rotated-secret" not in caplog.text and "Traceback" not in caplog.text
    finally:
        w.stop()


def test_on_change_needs_a_watched_input(svc: Path) -> None:
    s = docuconf.load(Svc, watch_interval=3600)
    w = watcher(s)
    try:
        with pytest.raises(KeyError, match="keystore_password"):
            w.on_change("keystore_password", print)
        with pytest.raises(KeyError):
            w.status("nope")
    finally:
        w.stop()


def test_status(svc: Path) -> None:
    s = docuconf.load(Svc, watch_interval=3600)
    w = watcher(s)
    try:
        assert w.status("token") == ReloadStatus("token", 1, None, None)
        assert sorted(w.statuses()) == ["keystore", "token"]

        before = datetime.now().astimezone()
        swap(svc / "etc/svc/token", "token-two")
        w.check_now()
        st = w.status("token")
        assert st.generation == 2 and st.last_rejected is None
        assert st.last_reload is not None and st.last_reload >= before

        swap(svc / "etc/svc/token", "bad")
        w.check_now()
        st = w.status("token")
        assert st.generation == 2 and st.last_reload is not None
        assert st.last_rejected is not None
        assert (st.last_rejected.input, st.last_rejected.codes) == ("token", ("pattern_mismatch",))
        assert st.last_rejected.time >= st.last_reload
        assert "bad" not in repr(st)

        # A later accepted change clears it.
        swap(svc / "etc/svc/token", "token-three")
        w.check_now()
        st = w.status("token")
        assert (st.generation, st.last_rejected) == (3, None)
        assert w.status("keystore").generation == 1
    finally:
        w.stop()


def test_keystore_reload_reuses_the_boot_password(svc: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = docuconf.load(Svc, watch_interval=3600)
    w = watcher(s)
    ks = s.keystore
    first = ks.certificate
    changed: list[Keystore] = []
    ks.on_change(changed.append)
    try:
        # The variable changing in this process does not matter: the reload uses the password read at boot.
        monkeypatch.setenv("SVC_KEYSTORE_PASSWORD", "rotated")
        second = make_leaf(["second.client"])
        write_p12(svc / "etc/svc/ks/client.p12.new", second, b"changeit")
        os.replace(svc / "etc/svc/ks/client.p12.new", svc / "etc/svc/ks/client.p12")
        assert w.check_now() == ["keystore"]
        assert s.keystore is ks and ks.certificate == second.cert and changed == [ks]

        # A keystore under a new password is rejected; the previous value is kept.
        write_p12(svc / "etc/svc/ks/client.p12.new", make_leaf(["third.client"]), b"rotated")
        os.replace(svc / "etc/svc/ks/client.p12.new", svc / "etc/svc/ks/client.p12")
        assert w.check_now() == []
        assert ks.certificate == second.cert and ks.certificate != first and changed == [ks]
        rejected = w.status("keystore").last_rejected
        assert rejected is not None and rejected.codes == ("keystore_unreadable",)
        assert w.status("keystore").generation == 2
    finally:
        w.stop()


# -- contract-first mode --------------------------------------------------------


def contract(root: Path) -> dict[str, Any]:
    return {
        "apiVersion": "docuconf.dev/v1alpha1",
        "kind": "ConfigContract",
        "metadata": {"name": "svc"},
        "vars": {
            "PAGE_SIZE": {
                "type": "int",
                "description": "Items per page",
                "min": 1,
                "max": 100,
                "default": 20,
                "configKey": "page.size",
            },
        },
        "files": {
            "motd": {
                "type": "text",
                "description": "Message of the day",
                "path": "/etc/svc/motd",
                "required": True,
                "minLength": 1,
                "reload": "watch",
            },
        },
        "overlays": {
            "platform": {"format": "json", "path": "/app/config/svc.json", "keySeparator": ".", "reload": "watch"},
        },
    }


def test_contract_first_reloads_watched_inputs(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "etc/svc").mkdir(parents=True)
    (root / "etc/svc/motd").write_text("hello")
    (root / "app/config").mkdir(parents=True)
    (root / "app/config/svc.json").write_text(json.dumps({"page": {"size": 30}}))
    env = {"DOCUCONF_FILE_ROOT": str(root)}

    assert docuconf.get_watcher(docuconf.load_contract(contract(root), env)) is None  # off by default with env=

    values = docuconf.load_contract(contract(root), env, watch=True, watch_interval=3600)
    w = watcher(values)
    assert (w.inputs, w.overlays) == (["motd"], ["platform"])
    motd: list[str] = []
    platform: list[dict[str, Any]] = []
    w.on_change("motd", motd.append)
    w.on_change("platform", platform.append)
    try:
        assert values.value("motd") == "hello" and values.value("PAGE_SIZE") == 30
        swap(root / "etc/svc/motd", "goodbye")
        swap(root / "app/config/svc.json", json.dumps({"page": {"size": 40}}))
        assert sorted(w.check_now()) == ["motd", "platform"]
        assert values.value("motd") == "goodbye" and values.value("PAGE_SIZE") == 40
        assert motd == ["goodbye"] and platform == [{"PAGE_SIZE": 40}]
        assert w.status("motd").generation == 2 and w.status("platform").generation == 2

        # Rejected changes keep the previous values.
        swap(root / "etc/svc/motd", "")
        swap(root / "app/config/svc.json", json.dumps({"page": {"size": 1000}}))
        assert w.check_now() == []
        assert values.value("motd") == "goodbye" and values.value("PAGE_SIZE") == 40
        assert motd == ["goodbye"] and len(platform) == 1
        motd_rejected, platform_rejected = w.status("motd").last_rejected, w.status("platform").last_rejected
        assert motd_rejected is not None and motd_rejected.codes == ("out_of_range",)
        assert platform_rejected is not None and platform_rejected.codes == ("out_of_range",)
    finally:
        w.stop()
