"""``reload="watch"``: poll file inputs and overlays and reload them in place
(SPEC §4.6.2, §4.7, §11.2 items 8 and 9).

Kubernetes updates projected volumes by swapping a ``..data`` symlink, so a
change shows up as a new inode, size or mtime behind the same path. The
watcher stats every file of a watched input (following symlinks), and when
any changed, re-reads and re-checks all of that input's files together. A
changed overlay re-validates the whole settings class through
pydantic-settings, and the fields whose values changed are replaced on the
settings object. A reload that fails its checks is logged and the previous
values are kept.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings

from .declaration import Declaration, FileSpec, OverlaySpec
from .errors import format_violations
from .files import FileResult, load_file
from .loader import Env, build
from .overlays import overlay_path
from .values import _FileValue

log = logging.getLogger("docuconf")

Listener = Callable[[str, Any], None]

_watchers: dict[int, Watcher] = {}


def _signature(paths: list[Path]) -> tuple[Any, ...]:
    sig: list[tuple[int, int, int] | None] = []
    for p in paths:
        try:
            st = os.stat(p)
            sig.append((st.st_ino, st.st_size, st.st_mtime_ns))
        except OSError:
            sig.append(None)
    return tuple(sig)


class Watcher:
    """Polls the watched file inputs of one settings instance."""

    def __init__(
        self,
        settings: BaseSettings,
        decl: Declaration,
        env: Env,
        results: list[FileResult],
        init: Mapping[str, Any] | None = None,
    ) -> None:
        self.settings = settings
        self.decl = decl
        self.env = env
        self.init = dict(init or {})
        self._listeners: list[Listener] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._watched: dict[str, tuple[FileSpec, list[Path], tuple[Any, ...]]] = {}
        for r in results:
            if r.spec.marker.reload == "watch":
                self._watched[r.spec.name] = (r.spec, r.paths, _signature(r.paths))
        self._overlays: dict[str, tuple[OverlaySpec, Path, tuple[Any, ...]]] = {}
        for o in decl.overlays:
            if o.reload == "watch":
                p = overlay_path(o)
                self._overlays[o.name] = (o, p, _signature([p]))

    @classmethod
    def start(
        cls,
        settings: BaseSettings,
        decl: Declaration,
        env: Env,
        results: list[FileResult],
        *,
        init: Mapping[str, Any] | None = None,
        interval: float,
    ) -> Watcher:
        w = cls(settings, decl, env, results, init)
        _watchers[id(settings)] = w
        t = threading.Thread(target=w._run, args=(interval,), name="docuconf-watch", daemon=True)
        w._thread = t
        t.start()
        return w

    @property
    def inputs(self) -> list[str]:
        """Watched file inputs."""
        return sorted(self._watched)

    @property
    def overlays(self) -> list[str]:
        """Watched overlays."""
        return sorted(self._overlays)

    def on_reload(self, listener: Listener) -> Callable[[], None]:
        """Call ``listener(name, new_value)`` after each successful reload.

        For a file input, ``new_value`` is the input's value. For an overlay,
        it is a dict of the fields whose values changed (field name to new
        value), which may be empty when the environment overrides them.
        """
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        _watchers.pop(id(self.settings), None)

    def _run(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                self.check_now()
            except Exception:
                log.exception("docuconf: file watch failed")

    def check_now(self, now: datetime | None = None) -> list[str]:
        """Check every watched input and overlay once; returns the names reloaded."""
        reloaded = []
        with self._lock:
            reloaded.extend(self._check_files(now))
            reloaded.extend(self._check_overlays())
        return reloaded

    def _notify(self, name: str, value: Any) -> None:
        for listener in list(self._listeners):
            try:
                listener(name, value)
            except Exception:
                log.exception("docuconf: reload listener failed")

    def _check_overlays(self) -> list[str]:
        changed = []
        for name, (spec, path, sig) in list(self._overlays.items()):
            current = _signature([path])
            if current != sig:
                self._overlays[name] = (spec, path, current)
                changed.append(name)
        if not changed:
            return []
        decl = self.decl
        file_fields = {f.field_name for f in decl.files} | {f.init_key for f in decl.files}
        # File inputs keep their current values; the watcher reloads them on their own.
        file_kwargs = {f.init_key: getattr(self.settings, f.field_name) for f in decl.files}
        new, violations = build(type(self.settings), decl, self.env, file_kwargs, file_fields, self.init, warn=False)
        if violations or new is None:
            log.error(
                "docuconf: keeping the previous values after a failed reload of overlay %s\n%s",
                ", ".join(changed),
                format_violations(violations),
            )
            return []
        changes: dict[str, Any] = {}
        for fname in type(self.settings).model_fields:
            if fname in file_fields:
                continue
            value = getattr(new, fname)
            if getattr(self.settings, fname) != value:
                object.__setattr__(self.settings, fname, value)
                changes[fname] = value
        for name in changed:
            log.info("docuconf: reloaded overlay %s", name)
            self._notify(name, dict(changes))
        return changed

    def _check_files(self, now: datetime | None) -> list[str]:
        reloaded = []
        for name, (spec, paths, sig) in list(self._watched.items()):
            current = _signature(paths)
            if current == sig:
                continue
            r = load_file(spec, self.env, now=now or datetime.now(timezone.utc))
            self._watched[name] = (spec, r.paths, _signature(r.paths))
            if r.violations or r.value is None:
                log.error(
                    "docuconf: keeping the previous %s after a failed reload\n%s",
                    name,
                    format_violations(r.violations) if r.violations else "docuconf: file input is gone",
                )
                continue
            old = getattr(self.settings, spec.field_name, None)
            if isinstance(old, _FileValue) and type(old) is type(r.value):
                old._replace(r.value)  # type: ignore[attr-defined]
                value = old
            else:
                object.__setattr__(self.settings, spec.field_name, r.value)
                value = r.value
            log.info("docuconf: reloaded %s", name)
            reloaded.append(name)
            self._notify(name, value)
        return reloaded


def get_watcher(settings: BaseSettings) -> Watcher | None:
    """The watcher :func:`docuconf.load` started for ``settings``, if any input or overlay uses ``reload="watch"``."""
    return _watchers.get(id(settings))
