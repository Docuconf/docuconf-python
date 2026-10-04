"""``reload="watch"``: poll file inputs and reload them in place (SPEC §4.6.2, §11.2 item 8).

Kubernetes updates projected volumes by swapping a ``..data`` symlink, so a
change shows up as a new inode, size or mtime behind the same path. The
watcher stats every file of a watched input (following symlinks), and when
any changed, re-reads and re-checks all of that input's files together. A
reload that fails its checks is logged and the previous value is kept.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings

from .declaration import Declaration, FileSpec
from .errors import format_violations
from .files import EnvLookup, FileResult, load_file
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

    def __init__(self, settings: BaseSettings, decl: Declaration, env: EnvLookup, results: list[FileResult]) -> None:
        self.settings = settings
        self.decl = decl
        self.env = env
        self._listeners: list[Listener] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._watched: dict[str, tuple[FileSpec, list[Path], tuple[Any, ...]]] = {}
        for r in results:
            if r.spec.marker.reload == "watch":
                self._watched[r.spec.name] = (r.spec, r.paths, _signature(r.paths))

    @classmethod
    def start(
        cls,
        settings: BaseSettings,
        decl: Declaration,
        env: EnvLookup,
        results: list[FileResult],
        *,
        interval: float,
    ) -> Watcher:
        w = cls(settings, decl, env, results)
        _watchers[id(settings)] = w
        t = threading.Thread(target=w._run, args=(interval,), name="docuconf-watch", daemon=True)
        w._thread = t
        t.start()
        return w

    @property
    def inputs(self) -> list[str]:
        return sorted(self._watched)

    def on_reload(self, listener: Listener) -> Callable[[], None]:
        """Call ``listener(input_name, new_value)`` after each successful reload."""
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
        """Check every watched input once; returns the names reloaded."""
        reloaded = []
        with self._lock:
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
                for listener in list(self._listeners):
                    try:
                        listener(name, value)
                    except Exception:
                        log.exception("docuconf: reload listener failed")
        return reloaded


def get_watcher(settings: BaseSettings) -> Watcher | None:
    """The watcher :func:`docuconf.load` started for ``settings``, if any input uses ``reload="watch"``."""
    return _watchers.get(id(settings))
