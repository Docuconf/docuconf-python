"""Config-file overlays (SPEC §4.7), loaded with pydantic-settings' own config file sources.

An overlay is one more config file, mounted by the platform, that sits
between the files the app ships with and the environment::

    defaults < baked-in config files < overlay < .env / environment < constructor arguments

pydantic-settings layers sources through ``settings_customise_sources``, so
the app keeps that hook and passes its sources through :func:`with_overlays`,
which inserts a ``JsonConfigSettingsSource``, ``YamlConfigSettingsSource`` or
``TomlConfigSettingsSource`` for each declared overlay in that position.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import sys
import weakref
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    InitSettingsSource,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
    TomlConfigSettingsSource,
    YamlConfigSettingsSource,
)

from . import _context
from .declaration import OverlaySpec, declaration
from .errors import DeclarationError, Violation


class _Unset:
    pass


_UNSET = _Unset()

_CONFIG_FILE_SOURCES = (JsonConfigSettingsSource, YamlConfigSettingsSource, TomlConfigSettingsSource)
_FILE_PATH_ATTRS = ("json_file_path", "yaml_file_path", "toml_file_path")
#: Marks the sources docuconf adds, with the overlay's name.
_MARK = "docuconf_overlay"

# Classes whose settings_customise_sources went through with_overlays.
_wired: weakref.WeakSet[type] = weakref.WeakSet()
# docuconf.load has already reported a malformed overlay; let the build go on without it.
_lenient: contextvars.ContextVar[bool] = contextvars.ContextVar("docuconf_overlays_lenient", default=False)


def overlay_path(o: OverlaySpec, root: str | _Unset | None = _UNSET) -> Path:
    """The overlay's path, under the file root (``DOCUCONF_FILE_ROOT``) if set."""
    r = _context.file_root() if isinstance(root, _Unset) else root
    return Path(r) / o.path.lstrip("/") if r else Path(o.path)


def _source(settings_cls: type[BaseSettings], o: OverlaySpec, path: Path | None = None) -> PydanticBaseSettingsSource:
    """pydantic-settings' own source for the overlay; reads the file (a missing file adds nothing)."""
    path = overlay_path(o) if path is None else path
    src: PydanticBaseSettingsSource
    if o.format == "json":
        src = JsonConfigSettingsSource(settings_cls, json_file=path, json_file_encoding="utf-8-sig")
    elif o.format == "yaml":
        src = YamlConfigSettingsSource(settings_cls, yaml_file=path, yaml_file_encoding="utf-8-sig")
    else:
        src = TomlConfigSettingsSource(settings_cls, toml_file=path)
    setattr(src, _MARK, o.name)
    return src


def _data(src: PydanticBaseSettingsSource, o: OverlaySpec) -> Any:
    return getattr(src, f"{o.format}_data")


def read_overlay(
    settings_cls: type[BaseSettings], o: OverlaySpec, root: str | None
) -> tuple[dict[str, Any], Violation | None]:
    """Read an overlay as its source would, turning read and parse errors into violations."""
    path = overlay_path(o, root)
    try:
        data = _data(_source(settings_cls, o, path), o)
    except ImportError as e:
        raise DeclarationError(
            [f"overlay {o.name}: format {o.format} needs {e.name or 'a parser'}; pip install 'docuconf-pydantic[yaml]'"]
        ) from e
    except PermissionError:
        return {}, Violation(o.name, "file", "file_unreadable", f"overlay at {path} cannot be read (permission denied)")
    except OSError as e:
        return {}, Violation(o.name, "file", "file_unreadable", f"overlay at {path} cannot be read: {e.strerror or e}")
    except Exception as e:  # each parser raises its own errors
        if "dictionary update sequence" in str(e):  # the file parsed, but not to a mapping
            return {}, Violation(o.name, "file", "file_malformed", f"overlay at {path} must hold a mapping")
        detail = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        return {}, Violation(o.name, "file", "file_malformed", f"overlay at {path} is not valid {o.format}: {detail}")
    if not isinstance(data, dict):
        return {}, Violation(o.name, "file", "file_malformed", f"overlay at {path} must hold a mapping")
    return data, None


@contextlib.contextmanager
def lenient() -> Iterator[None]:
    token = _lenient.set(True)
    try:
        yield
    finally:
        _lenient.reset(token)


def is_wired(settings_cls: type[BaseSettings]) -> bool:
    return settings_cls in _wired


def _shipped_files(settings_cls: type[BaseSettings], sources: Sequence[PydanticBaseSettingsSource]) -> list[Path]:
    """Files the app ships with: its baked-in config files and the module declaring the settings."""
    out: list[Path] = []
    for s in sources:
        if not isinstance(s, _CONFIG_FILE_SOURCES) or hasattr(s, _MARK):
            continue
        for attr in _FILE_PATH_ATTRS:
            v = getattr(s, attr, None)
            if v is None:
                continue
            for p in [v] if isinstance(v, (str, os.PathLike)) else list(v):
                out.append(Path(p).expanduser())
    module = sys.modules.get(settings_cls.__module__)
    module_file = getattr(module, "__file__", None)
    if module_file:
        out.append(Path(module_file))
    return out


def _check_not_over_shipped(
    settings_cls: type[BaseSettings], overlays: Sequence[OverlaySpec], sources: Sequence[PydanticBaseSettingsSource]
) -> None:
    # The platform mounts the overlay's directory, which hides whatever the image has there (SPEC §4.7).
    problems: list[str] = []
    cwd = Path.cwd().resolve()
    shipped = [(p, p.resolve()) for p in _shipped_files(settings_cls, sources)]
    for o in overlays:
        d = overlay_path(o).parent.resolve()
        if d == cwd or d in cwd.parents:
            problems.append(
                f"overlay {o.name}: {o.path} is in the app's working directory {cwd}; mounting it would hide the "
                "app's files. Use a directory of its own, such as /app/config."
            )
            continue
        for p, real in shipped:
            if d in real.parents:
                problems.append(
                    f"overlay {o.name}: mounting {d} would hide {p}, which the app ships with. "
                    "Use a directory of its own, such as /app/config."
                )
                break
    if problems:
        raise DeclarationError(problems)


def with_overlays(
    settings_cls: type[BaseSettings], *sources: PydanticBaseSettingsSource
) -> tuple[PydanticBaseSettingsSource, ...]:
    """Insert the declared overlays into pydantic-settings' sources, in SPEC §4.7 order.

    Call it from ``settings_customise_sources`` with the sources in the usual
    order (highest priority first). Each overlay goes just before the first
    baked-in config file source (``JsonConfigSettingsSource``,
    ``YamlConfigSettingsSource``, ``TomlConfigSettingsSource``), or last when
    there is none, so it beats baked-in files and defaults while the
    environment (and ``.env``) beat it::

        @classmethod
        def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings,
                                       file_secret_settings):
            return docuconf.with_overlays(
                settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings,
                YamlConfigSettingsSource(settings_cls, yaml_file="settings.yaml"),
            )

    Raises :class:`DeclarationError` when an environment source comes after a
    baked-in file (the overlay could not sit between them), or when an
    overlay's directory would hide files the app ships with.
    """
    overlays = declaration(settings_cls).overlays
    if not overlays:
        return tuple(sources)
    srcs = list(sources)
    at = next(
        (i for i, s in enumerate(srcs) if isinstance(s, _CONFIG_FILE_SOURCES) and not hasattr(s, _MARK)), len(srcs)
    )
    if any(isinstance(s, EnvSettingsSource) for s in srcs[at:]):
        raise DeclarationError(
            [
                "overlays load between baked-in config files and the environment (SPEC §4.7), so environment "
                "sources must come before config file sources in settings_customise_sources"
            ]
        )
    _check_not_over_shipped(settings_cls, overlays, srcs)
    added: list[PydanticBaseSettingsSource] = []
    for o in overlays:
        try:
            added.append(_source(settings_cls, o))
        except Exception:
            if not _lenient.get():
                raise
            # docuconf.load reports the malformed file; the other inputs are still checked.
            empty = InitSettingsSource(settings_cls, init_kwargs={})
            setattr(empty, _MARK, o.name)
            added.append(empty)
    _wired.add(settings_cls)
    return (*srcs[:at], *added, *srcs[at:])
