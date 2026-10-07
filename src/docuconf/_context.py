"""State shared by the loader and the pydantic-settings sources it installs.

docuconf never changes ``os.environ``. While it builds a settings object, the
environment it read (the process environment, or the mapping passed as
``load(..., env=...)``) is held here, and the sources docuconf puts in
``settings_customise_sources`` read it from here instead of ``os.environ``.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Active:
    #: The environment pydantic-settings sees, with case folding applied.
    values: Mapping[str, str | None]
    #: Directory prepended to absolute file paths (``DOCUCONF_FILE_ROOT``).
    file_root: str | None


_active: contextvars.ContextVar[Active | None] = contextvars.ContextVar("docuconf_active", default=None)

#: Set by ``docuconf export`` while it imports the settings module, so a module-level
#: ``load()`` does not validate an environment that is not there (see ``loader.load``).
exporting: contextvars.ContextVar[bool] = contextvars.ContextVar("docuconf_exporting", default=False)


@contextlib.contextmanager
def activate(values: Mapping[str, str | None], file_root: str | None) -> Iterator[None]:
    token = _active.set(Active(values, file_root))
    try:
        yield
    finally:
        _active.reset(token)


def active() -> Active | None:
    return _active.get()


def file_root() -> str | None:
    """``DOCUCONF_FILE_ROOT`` for the build in progress, or from the process environment."""
    a = _active.get()
    if a is not None:
        return a.file_root
    return os.environ.get("DOCUCONF_FILE_ROOT") or None
