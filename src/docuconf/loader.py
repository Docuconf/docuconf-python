"""Boot-time loading: pydantic-settings parses, docuconf checks (SPEC §11.2)."""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from typing import Any, TypeVar, cast

from pydantic import ValidationError as PydanticValidationError
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsError,
)
from typing_extensions import Self

from .declaration import _MISSING, Declaration, VarSpec, declaration
from .errors import ConfigValidationError, DeclarationError, ErrorCode, Violation, write_termination_log
from .files import FileResult, load_file
from .overlays import is_wired, lenient, read_overlay, with_overlays

log = logging.getLogger("docuconf")

S = TypeVar("S", bound=BaseSettings)

INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1

_RANGE = {"greater_than", "greater_than_equal", "less_than", "less_than_equal", "string_too_short", "string_too_long"}
_CODES: dict[str, ErrorCode] = {
    "missing": "missing_required",
    "string_pattern_mismatch": "pattern_mismatch",
    "literal_error": "not_in_enum",
    "enum": "not_in_enum",
    "url_scheme": "invalid_scheme",
    "schema_mismatch": "schema_mismatch",
}
# pydantic messages that never include the input, so are safe for secrets.
_SAFE_FOR_SECRETS = (
    _RANGE
    | set(_CODES)
    | {
        "string_type",
        "url_parsing",
        "url_syntax_violation",
        "url_type",
        "url_too_long",
        "int_parsing",
        "float_parsing",
        "bool_parsing",
        "time_delta_parsing",
        "duration_parsing",
        "json_invalid",
        "too_short",
        "too_long",
    }
)


class Env:
    """The environment as pydantic-settings sees it (case folding, opt-in .env file).

    With ``values``, the environment is that mapping alone, as in the
    contract-first mode, whose settings source reads :attr:`values`.
    """

    def __init__(self, cls: type[BaseSettings], decl: Declaration, values: Mapping[str, str] | None = None) -> None:
        self.case_sensitive = decl.case_sensitive
        self.own = values is not None
        if values is not None:
            self.values: dict[str, str | None] = {
                (k if self.case_sensitive else k.lower()): v for k, v in values.items()
            }
            return
        loaded: dict[str, str | None] = {}
        if decl.env_file:
            # pydantic-settings' own dotenv reader; real environment variables win.
            loaded.update(DotEnvSettingsSource(cls).env_vars)
        loaded.update(EnvSettingsSource(cls).env_vars)
        self.values = loaded

    @contextlib.contextmanager
    def hidden(self, names: set[str]) -> Iterator[None]:
        """Hide variables from pydantic-settings while the settings class is built.

        pydantic-settings raises (rather than reporting a validation error) when
        a value it decodes as JSON is malformed. docuconf reports that variable
        itself, then hides it so every other variable is still checked.
        """
        fold = (lambda n: n) if self.case_sensitive else str.lower
        wanted = {fold(n) for n in names}
        store: dict[str, str | None] | os._Environ[str] = self.values if self.own else os.environ
        saved = {k: store[k] for k in list(store) if fold(k) in wanted}
        for k in saved:
            del store[k]
        try:
            yield
        finally:
            for k, v in saved.items():
                store[k] = v  # type: ignore[assignment]

    def __call__(self, name: str) -> str | None:
        return self.values.get(name if self.case_sensitive else name.lower())

    def first(self, names: tuple[str, ...]) -> str | None:
        for n in names:
            v = self(n)
            if v is not None:
                return v
        return None

    def indexed(self, names: tuple[str, ...]) -> tuple[list[str] | None, int | None]:
        """The items of an ``indexed`` list (SPEC §5) under the first name that has any, or the first missing index.

        Only ``NAME__<n>`` with a decimal ``<n>`` and no leading zero is an
        item; ``NAME__HOST`` is not. Items must run from 0 with no gap.
        """
        for name in names:
            prefix = f"{name}__" if self.case_sensitive else f"{name}__".lower()
            items: dict[int, str] = {}
            for key, value in self.values.items():
                if value is not None and key.startswith(prefix) and _INDEX.fullmatch(key[len(prefix) :]):
                    items[int(key[len(prefix) :])] = value
            if items:
                gap = next(i for i in range(len(items) + 1) if i not in items)
                if gap < len(items):
                    return None, gap
                return [items[i] for i in range(len(items))], None
        return None, None


#: An item index of an ``indexed`` list: decimal, no leading zero.
_INDEX = re.compile(r"0|[1-9][0-9]*")


def _set_path(d: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    for k in path[:-1]:
        d = d.setdefault(k, {})
    d[path[-1]] = value


#: Reference schemes of injectors that resolve env values when the process starts (SPEC §4.5.1).
INJECTOR_SCHEMES = ("vault:", "op://", "ref+")


def unresolved_reference(raw: str) -> str | None:
    """The injector scheme ``raw`` starts with, if it is still an unresolved reference."""
    return next((s for s in INJECTOR_SCHEMES if raw.startswith(s)), None)


def _precheck(v: VarSpec, raw: str, init: dict[str, Any], out: list[Violation], hide: set[str]) -> bool:
    """SPEC §5 rules the host does not apply. Returns False if the var is handled.

    Adds to ``hide`` the variables pydantic-settings must not see (malformed JSON).
    """
    if raw == "" and v.type != "string":
        # Empty means unset for every type but string.
        if v.required:
            out.append(Violation(v.name, "var", "missing_required", "required, but set to an empty string"))
        elif v.py_default is not _MISSING:
            _set_path(init, v.alias_loc, v.py_default)
        return False
    if v.type == "int":
        try:
            n = int(raw)
        except ValueError:
            return True
        if not INT64_MIN <= n <= INT64_MAX:
            out.append(Violation(v.name, "var", "out_of_range", "outside the 64-bit signed integer range"))
            return False
    if v.type == "float":
        try:
            f = float(raw)
        except ValueError:
            return True
        if not math.isfinite(f):
            out.append(Violation(v.name, "var", "invalid_type", "must be a finite number"))
            return False
    encoding = v.attrs.get("encoding")
    if v.type == "json" or (v.type == "list" and encoding in ("json", "indexed")):
        try:
            data = json.loads(raw, parse_constant=_reject_constant)
        except ValueError:
            out.append(Violation(v.name, "var", "invalid_type", "not valid JSON"))
            hide.update(v.env_names)
            return False
        if v.type == "list" and v.attrs.get("items") == "int" and isinstance(data, list):
            return _check_int_items(v, data, out, strict=encoding == "json")
    if v.type == "list" and encoding == "csv" and v.attrs.get("items") == "int":
        return _check_int_items(v, raw.split(v.attrs.get("separator", ",")), out, strict=False)
    return True


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _check_int_items(v: VarSpec, items: list[Any], out: list[Violation], *, strict: bool) -> bool:
    """Items of an int list: JSON integers (``strict``), and within 64 bits (SPEC §5)."""
    for i, item in enumerate(items):
        if isinstance(item, str) and not strict:
            try:
                item = int(item)
            except ValueError:
                continue  # pydantic reports it
        if not isinstance(item, int) or isinstance(item, bool):
            out.append(Violation(v.name, "var", "invalid_type", f"item {i} is not an integer"))
            return False
        if not INT64_MIN <= item <= INT64_MAX:
            out.append(Violation(v.name, "var", "out_of_range", f"item {i} is outside the 64-bit signed integer range"))
            return False
    return True


def _code(v: VarSpec | None, err: Mapping[str, Any], nested: bool) -> ErrorCode:
    t = err["type"]
    if v is not None and v.type == "list" and t in ("too_short", "too_long"):
        return "too_few_items" if t == "too_short" else "too_many_items"
    if v is not None and v.type == "json" and nested and t != "missing":
        return "schema_mismatch"
    if t in _RANGE:
        return "out_of_range"
    return _CODES.get(t, "invalid_type")


def _message(v: VarSpec | None, err: Mapping[str, Any], nested_loc: tuple[Any, ...]) -> str:
    t = err["type"]
    if t == "missing":
        return "required, but not set"
    prefix = ".".join(str(p) for p in nested_loc)
    prefix = f"{prefix}: " if prefix else ""
    secret = v is not None and v.secret
    if secret and t not in _SAFE_FOR_SECRETS:
        return f"{prefix}invalid value (secret, not shown)"
    msg = f"{prefix}{err['msg']}"
    inp = err.get("input")
    if not secret and isinstance(inp, str) and not nested_loc:
        shown = inp if len(inp) <= 60 else inp[:57] + "..."
        msg += f" (got {shown!r})"
    return msg


def _violations_from(
    decl: Declaration, e: PydanticValidationError, skip: set[str], origin: Mapping[str, str]
) -> list[Violation]:
    out: list[Violation] = []
    seen: set[tuple[str, str]] = set()
    for err in e.errors(include_url=False):
        loc = tuple(str(p) for p in err["loc"])
        if loc and loc[0] in skip:
            continue
        v = None
        nested: tuple[str, ...] = ()
        for i in range(len(loc), 0, -1):
            v = decl.by_loc.get(loc[:i])
            if v is not None:
                nested = loc[i:]
                break
        if v is not None:
            if v.name in skip:
                continue
            code = _code(v, err, bool(nested))
            key = (v.name, code)
            if code == "missing_required" and key in seen:
                continue
            seen.add(key)
            msg = _message(v, err, nested)
            if v.name in origin and code != "missing_required":
                msg += f" (from overlay {origin[v.name]})"
            out.append(Violation(v.name, "var", code, msg))
        elif err["type"] == "missing" and (
            nested_required := [
                n
                for n in decl.vars
                if n.required and loc and (n.loc[: len(loc)] == loc or n.alias_loc[: len(loc)] == loc)
            ]
        ):
            # A required nested model with env_nested_delimiter: report its variables.
            for n in nested_required:
                if n.name not in skip and (n.name, "missing_required") not in seen:
                    seen.add((n.name, "missing_required"))
                    out.append(Violation(n.name, "var", "missing_required", "required, but not set"))
        else:
            # A model validator, or a field docuconf does not export (Exclude).
            name = ".".join(loc) or decl.settings_cls.__name__
            out.append(Violation(name, "model", _code(None, err, False), _message(None, err, loc[1:])))
    return out


def _lookup(data: Mapping[str, Any], path: tuple[str, ...]) -> tuple[bool, Any]:
    cur: Any = data
    for k in path:
        if not isinstance(cur, Mapping) or k not in cur:
            return False, None
        cur = cur[k]
    return True, cur


def _precheck_native(v: VarSpec, value: Any, overlay: str, out: list[Violation]) -> bool:
    """SPEC §5 number rules for a typed value from an overlay. Returns False if the var is handled."""
    where = f" (from overlay {overlay})"
    is_int = v.type == "int" and isinstance(value, int) and not isinstance(value, bool)
    if is_int and not INT64_MIN <= value <= INT64_MAX:
        out.append(Violation(v.name, "var", "out_of_range", "outside the 64-bit signed integer range" + where))
        return False
    if v.type == "float" and isinstance(value, float) and not math.isfinite(value):
        out.append(Violation(v.name, "var", "invalid_type", "must be a finite number" + where))
        return False
    return True


def _first_line(e: Exception) -> str:
    return str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__


def _deprecated(v: VarSpec) -> None:
    if "deprecated" in v.common:
        dep = v.common["deprecated"]
        repl = f"; use {dep['replacedBy']}" if "replacedBy" in dep else ""
        log.warning("docuconf: %s is deprecated: %s%s", v.name, dep["message"], repl)


def build(
    cls: type[S],
    decl: Declaration,
    env: Env,
    file_kwargs: Mapping[str, Any],
    file_fields: set[str],
    init: Mapping[str, Any],
    *,
    warn: bool = True,
) -> tuple[S | None, list[Violation]]:
    """Check the variables and overlays, then instantiate ``cls``; file inputs are already loaded."""
    violations: list[Violation] = []
    kwargs: dict[str, Any] = {}
    skip = set(file_fields)
    hide: set[str] = set()

    overlay_data: list[tuple[str, dict[str, Any]]] = []
    for o in decl.overlays:
        data, bad = read_overlay(cls, o)
        if bad is not None:
            violations.append(bad)
        overlay_data.append((o.name, data))

    origin: dict[str, str] = {}
    indexed: dict[str, list[str]] = {}
    for v in decl.vars:
        raw = env.first(v.env_names)
        if v.type == "list" and v.attrs.get("encoding") == "indexed":
            # pydantic-settings cannot read NAME__0, NAME__1...: docuconf gathers the items and passes them in.
            # NAME itself is not part of the list, so pydantic-settings must not read it either.
            hide.update(v.env_names)
            items, gap = env.indexed(v.env_names)
            if gap is not None:
                msg = f"items must be numbered from 0 with no gap, but {v.name}__{gap} is not set"
                violations.append(Violation(v.name, "var", "invalid_type", msg))
                skip.add(v.name)
                continue
            raw = None if items is None else json.dumps(items)
            if items is not None:
                indexed[v.name] = items
        in_overlay = None
        for name, data in overlay_data:
            found, value = _lookup(data, v.alias_loc)
            if found:
                in_overlay = (name, value)
                break
        if raw is None:
            if in_overlay is None:
                continue
            origin[v.name] = in_overlay[0]
            if warn:
                _deprecated(v)
            if not _precheck_native(v, in_overlay[1], in_overlay[0], violations):
                skip.add(v.name)
            continue
        if in_overlay is not None and warn:
            log.warning(
                "docuconf: %s is set in the environment and in overlay %s; the environment wins", v.name, in_overlay[0]
            )
        if warn:
            _deprecated(v)
        if raw == "" and v.type != "string" and in_overlay is not None:
            # Empty means unset (SPEC §5), so the overlay's value applies.
            origin[v.name] = in_overlay[0]
            _set_path(kwargs, v.alias_loc, in_overlay[1])
            continue
        scheme = unresolved_reference(raw) if v.secret else None
        if scheme is not None:
            # SPEC §11.2: the injector did not run. Name the scheme, never the value.
            violations.append(
                Violation(
                    v.name,
                    "var",
                    "invalid_type",
                    f"holds an unresolved {scheme} reference; the injector that should resolve it did not run",
                )
            )
            skip.add(v.name)
            continue
        if not _precheck(v, raw, kwargs, violations, hide) and violations and violations[-1].input == v.name:
            skip.add(v.name)
        elif v.name in indexed:
            _set_path(kwargs, v.alias_loc, indexed[v.name])

    kwargs.update(file_kwargs)
    kwargs.update(init)
    settings: S | None = None
    try:
        with lenient(), env.hidden(hide):
            settings = cls(**kwargs)
    except PydanticValidationError as e:
        violations.extend(_violations_from(decl, e, skip, origin))
    except SettingsError as e:
        # A value pydantic-settings could not decode that docuconf did not hide (from a .env file, say).
        if not violations:
            violations.append(Violation(cls.__name__, "model", "invalid_type", _first_line(e)))
    if decl.overlays and not is_wired(cls):
        raise DeclarationError(
            [
                f"{cls.__name__} declares overlays, but settings_customise_sources does not load them: return "
                "docuconf.with_overlays(settings_cls, init_settings, env_settings, dotenv_settings, "
                "file_secret_settings, ...), or mix in docuconf.DocuconfSettings"
            ]
        )
    return settings, violations


def load(
    cls: type[S],
    *,
    watch: bool = True,
    watch_interval: float = 2.0,
    termination_log: str | bool | None = None,
    now: datetime | None = None,
    **init: Any,
) -> S:
    """Instantiate ``cls`` from the environment and check every input.

    pydantic-settings loads and parses as usual. docuconf adds the SPEC rules
    the host lacks (empty means unset for non-strings, 64-bit ints, finite
    floats, unresolved injector references in secrets), reads and checks every
    file input and config-file overlay, and raises one
    :class:`ConfigValidationError` listing every violation with its SPEC code.
    The message is also written to ``/dev/termination-log`` when it exists (or
    to ``DOCUCONF_TERMINATION_LOG``); pass ``termination_log=False`` to skip.

    File inputs and overlays declared with ``reload="watch"`` are polled every
    ``watch_interval`` seconds (see :func:`get_watcher`). Extra keyword
    arguments are passed to the settings constructor (they win over the
    environment, as in pydantic-settings).
    """
    decl = declaration(cls)
    for w in decl.warnings:
        log.debug("docuconf: %s", w)
    env = Env(cls, decl)
    violations: list[Violation] = []
    file_kwargs: dict[str, Any] = {}
    file_fields: set[str] = set()

    results: list[FileResult] = []
    for f in decl.files:
        r = load_file(f, env, now=now or datetime.now(timezone.utc))
        results.append(r)
        violations.extend(r.violations)
        file_fields.add(f.field_name)
        file_fields.add(f.init_key)
        if r.value is not None:
            file_kwargs[f.init_key] = r.value
        elif f.py_default is not _MISSING:
            file_kwargs[f.init_key] = f.py_default

    settings, more = build(cls, decl, env, file_kwargs, file_fields, init)
    violations.extend(more)

    if violations or settings is None:
        violations.sort(key=lambda v: (v.kind == "file", v.input))
        err = ConfigValidationError(violations)
        if termination_log is not False:
            write_termination_log(str(err), termination_log if isinstance(termination_log, str) else None)
        raise err

    if watch and (
        any(f.marker.reload == "watch" for f in decl.files) or any(o.reload == "watch" for o in decl.overlays)
    ):
        from .watch import Watcher

        Watcher.start(settings, decl, env, results, init=init, interval=watch_interval)
    return settings


class DocuconfSettings:
    """Mixin adding ``Settings.load()``, and loading declared overlays::

    class Settings(DocuconfSettings, BaseSettings): ...
    settings = Settings.load()

    Its ``settings_customise_sources`` keeps pydantic-settings' default
    sources and adds the overlays (see :func:`docuconf.with_overlays`).
    Override it to add baked-in config files.
    """

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return with_overlays(settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings)

    @classmethod
    def load(cls, **kwargs: Any) -> Self:
        if not issubclass(cls, BaseSettings):
            raise TypeError("DocuconfSettings must be mixed into a pydantic-settings BaseSettings class")
        return cast(Self, load(cls, **kwargs))
