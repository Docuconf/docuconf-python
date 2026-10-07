"""FastAPI integration: settings as a dependency, loaded and checked once at startup.

::

    from typing import Annotated
    from fastapi import Depends, FastAPI
    from docuconf.fastapi import settings_dependency

    get_settings = settings_dependency(Settings)
    app = FastAPI(lifespan=get_settings.lifespan)   # fails fast, cleanly, before serving

    @app.get("/port")
    def port(settings: Annotated[Settings, Depends(get_settings)]) -> int:
        return settings.port

In tests, override the dependency with settings loaded from a mapping; the
lifespan uses the override too, so the process environment is never read::

    app.dependency_overrides[get_settings] = lambda: Settings.load(env={"DATABASE_URL": "postgres://h/db"})

This module does not import FastAPI, so it adds no dependency.
"""

from __future__ import annotations

import contextlib
import sys
import threading
from collections.abc import AsyncIterator, Callable
from typing import Any, Generic, TypeVar

from pydantic_settings import BaseSettings

from .errors import ConfigValidationError, DeclarationError
from .loader import load

S = TypeVar("S", bound=BaseSettings)

__all__ = ["SettingsDependency", "settings_dependency"]


class SettingsDependency(Generic[S]):
    """A FastAPI dependency returning one settings object, loaded with :func:`docuconf.load` on first use."""

    def __init__(self, cls: type[S], *, exit_on_error: bool = True, **load_kwargs: Any) -> None:
        self.settings_cls = cls
        self.exit_on_error = exit_on_error
        self.load_kwargs = load_kwargs
        self._value: S | None = None
        self._lock = threading.Lock()

    def __call__(self) -> S:
        value = self._value
        if value is None:
            with self._lock:
                if self._value is None:
                    self._value = load(self.settings_cls, **self.load_kwargs)
                value = self._value
        return value

    def cache_clear(self) -> None:
        """Forget the loaded settings; the next call loads them again."""
        with self._lock:
            self._value = None

    def check(self, app: Any = None) -> S:
        """Load the settings now (or the override in ``app.dependency_overrides``), exiting cleanly if they are invalid.

        On a configuration problem it prints the ``docuconf: N configuration problems:`` block to stderr (the
        termination log is written by :func:`docuconf.load`) and exits the process with status 1, without the
        ASGI server's lifespan traceback. With ``exit_on_error=False`` it raises the error instead. Call it
        from your own lifespan, or use :attr:`lifespan`.
        """
        overrides: dict[Any, Callable[[], Any]] = getattr(app, "dependency_overrides", None) or {}
        getter: Callable[[], Any] = overrides.get(self, self)
        try:
            result: S = getter()
        except (ConfigValidationError, DeclarationError) as e:
            if not self.exit_on_error:
                raise
            print(e, file=sys.stderr, flush=True)
            # SystemExit raised inside an ASGI lifespan is caught and logged with a traceback by the server, and
            # the server then exits with its own status; leave at once with the documented status instead.
            sys.stderr.flush()
            _exit(1)
            raise SystemExit(1) from None  # only reached when _exit is replaced, in tests
        return result

    @contextlib.asynccontextmanager
    async def lifespan(self, app: Any) -> AsyncIterator[None]:
        """A FastAPI/Starlette ``lifespan`` that runs :meth:`check` before the app serves requests."""
        self.check(app)
        yield


def _exit(status: int) -> None:  # pragma: no cover - replaced in tests
    import os

    os._exit(status)


def settings_dependency(cls: type[S], *, exit_on_error: bool = True, **load_kwargs: Any) -> SettingsDependency[S]:
    """A dependency for ``Depends()`` that loads ``cls`` once, with :func:`docuconf.load` and ``load_kwargs``.

    ``exit_on_error`` (default true): its :attr:`~SettingsDependency.lifespan` exits the process with status 1
    on a configuration problem, rather than letting the ASGI server print a traceback and exit with status 3.
    """
    return SettingsDependency(cls, exit_on_error=exit_on_error, **load_kwargs)
