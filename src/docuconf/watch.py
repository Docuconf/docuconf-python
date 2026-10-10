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

Hooks (:meth:`Watcher.on_change`, :meth:`Watcher.on_reload` and
``value.on_change``) run on the watcher thread, right after an accepted
reload, without the app reading the value. :meth:`Watcher.status` reports each
watched input's generation, last accepted reload and last rejected change.
"""

from __future__ import annotations

import logging
import os
import threading
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings

from .declaration import Declaration, FileSpec, OverlaySpec
from .errors import ErrorCode, Violation, format_violations
from .files import FileResult, load_file
from .loader import Env, build
from .overlays import overlay_path
from .values import _FileValue, call_hooks

log = logging.getLogger("docuconf")

Listener = Callable[[str, Any], None]
#: Rebuilds the settings for an overlay reload: (file kwargs, file fields) -> (settings or None, violations).
Rebuild = Callable[[dict[str, Any], set[str]], tuple[BaseSettings | None, list[Violation]]]


@dataclass(frozen=True)
class RejectedChange:
    """A change to a watched input that failed its checks; the previous value was kept.

    Holds the input name and the violation codes, never the content.
    """

    time: datetime
    input: str
    codes: tuple[ErrorCode, ...]


@dataclass(frozen=True)
class ReloadStatus:
    """The reload state of one watched input or overlay (for a health check or a metric)."""

    input: str
    #: 1 after boot, plus one per accepted reload.
    generation: int
    #: When the last accepted reload happened; ``None`` until the first one.
    last_reload: datetime | None
    #: The last rejected change; cleared when a later change is accepted.
    last_rejected: RejectedChange | None


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
        *,
        overlays: list[OverlaySpec] | None = None,
        rebuild: Rebuild | None = None,
    ) -> None:
        # Weak, so the watcher (and its thread) stops when the settings object is garbage-collected.
        self._settings = weakref.ref(settings)
        self._key = id(settings)
        self.decl = decl
        self.env = env
        self.init = dict(init or {})
        self._listeners: list[Listener] = []
        self._hooks: dict[str, list[Callable[[Any], None]]] = {}
        self._status: dict[str, ReloadStatus] = {}
        self._rebuild = rebuild
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._watched: dict[str, tuple[FileSpec, list[Path], tuple[Any, ...]]] = {}
        for r in results:
            if r.spec.marker.reload == "watch":
                self._watched[r.spec.name] = (r.spec, r.paths, _signature(r.paths))
        self._overlays: dict[str, tuple[OverlaySpec, Path, tuple[Any, ...]]] = {}
        for o in decl.overlays if overlays is None else overlays:
            if o.reload == "watch":
                p = overlay_path(o, env.file_root)
                self._overlays[o.name] = (o, p, _signature([p]))
        for name in [*self._watched, *self._overlays]:
            self._status[name] = ReloadStatus(name, 1, None, None)

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
        overlays: list[OverlaySpec] | None = None,
        rebuild: Rebuild | None = None,
    ) -> Watcher:
        w = cls(settings, decl, env, results, init, overlays=overlays, rebuild=rebuild)
        _watchers[id(settings)] = w
        weakref.finalize(settings, w._release)
        t = threading.Thread(target=w._run, args=(interval,), name="docuconf-watch", daemon=True)
        w._thread = t
        t.start()
        return w

    @property
    def settings(self) -> BaseSettings:
        """The settings object this watcher updates."""
        s = self._settings()
        if s is None:
            raise RuntimeError("docuconf: the settings object of this watcher was garbage-collected")
        return s

    def _release(self) -> None:
        # The settings object is gone: stop polling, without joining (this may run on the watcher thread).
        self._stop.set()
        if _watchers.get(self._key) is self:
            del _watchers[self._key]

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

    def on_change(self, name: str, hook: Callable[[Any], None]) -> Callable[[], None]:
        """Call ``hook(new_value)`` each time a new value of the watched input ``name`` is accepted.

        ``name`` is a watched file input or overlay, by its contract name. The
        hook runs on the watcher thread after the new value passed its checks
        and replaced the old one; never for a rejected change. For an overlay,
        ``new_value`` is a dict of the fields whose values changed. A hook that
        raises is logged with the input name and the exception type, and the
        other hooks and the reload go on. Returns a function that removes the
        hook. Raises ``KeyError`` when ``name`` is not watched.
        """
        if name not in self._status:
            raise KeyError(f"{name!r} is not a watched input; watched: {', '.join(sorted(self._status)) or 'none'}")
        hooks = self._hooks.setdefault(name, [])
        hooks.append(hook)

        def remove() -> None:
            if hook in hooks:
                hooks.remove(hook)

        return remove

    def status(self, name: str) -> ReloadStatus:
        """The reload status of the watched input or overlay ``name``; raises ``KeyError`` when it is not watched."""
        return self._status[name]

    def statuses(self) -> dict[str, ReloadStatus]:
        """The reload status of every watched input and overlay, by name."""
        return dict(self._status)

    def _accepted(self, name: str, now: datetime) -> None:
        prev = self._status[name]
        self._status[name] = ReloadStatus(name, prev.generation + 1, now, None)

    def _rejected(self, name: str, now: datetime, violations: list[Violation]) -> None:
        codes: tuple[ErrorCode, ...] = tuple(dict.fromkeys(v.code for v in violations)) or ("file_missing",)
        prev = self._status[name]
        self._status[name] = ReloadStatus(name, prev.generation, prev.last_reload, RejectedChange(now, name, codes))

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        if _watchers.get(self._key) is self:
            del _watchers[self._key]

    def _run(self, interval: float) -> None:
        while not self._stop.wait(interval):
            if self._settings() is None:
                return
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
        call_hooks(name, self._hooks.get(name, []), value)
        if isinstance(value, _FileValue):
            value._notify(name)
        call_hooks(name, self._listeners, name, value)

    def _check_overlays(self) -> list[str]:
        changed = []
        for name, (spec, path, sig) in list(self._overlays.items()):
            current = _signature([path])
            if current != sig:
                self._overlays[name] = (spec, path, current)
                changed.append(name)
        if not changed:
            return []
        at = datetime.now(timezone.utc)
        decl = self.decl
        file_fields = {f.field_name for f in decl.files} | {f.init_key for f in decl.files}
        # File inputs keep their current values; the watcher reloads them on their own.
        file_kwargs = {f.init_key: getattr(self.settings, f.field_name) for f in decl.files}
        if self._rebuild is not None:
            new, violations = self._rebuild(file_kwargs, file_fields)
        else:
            env = self.env
            if not env.own:
                # The process environment, as it is now.
                env = Env(type(self.settings), decl, None, env.file_root)
            new, violations = build(type(self.settings), decl, env, file_kwargs, file_fields, self.init, warn=False)
        if violations or new is None:
            for name in changed:
                self._rejected(name, at, violations)
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
            self._accepted(name, at)
        for name in changed:
            self._notify(name, dict(changes))
        return changed

    def _check_files(self, now: datetime | None) -> list[str]:
        reloaded = []
        for name, (spec, paths, sig) in list(self._watched.items()):
            current = _signature(paths)
            if current == sig:
                continue
            # self.env is the environment read at boot: a keystore password is not re-read.
            at = datetime.now(timezone.utc)
            r = load_file(spec, self.env, now=now or at, root=self.env.file_root)
            self._watched[name] = (spec, r.paths, _signature(r.paths))
            if r.violations or r.value is None:
                self._rejected(name, at, r.violations)
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
            self._accepted(name, at)
            reloaded.append(name)
            self._notify(name, value)
        return reloaded


def get_watcher(settings: BaseSettings) -> Watcher | None:
    """The watcher :func:`docuconf.load` started for ``settings``, if any input or overlay uses ``reload="watch"``."""
    w = _watchers.get(id(settings))
    return w if w is not None and w._settings() is settings else None
