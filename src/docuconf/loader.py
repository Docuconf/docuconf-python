"""Boot-time loading: pydantic-settings parses, docuconf checks (SPEC §11.2)."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, TypeVar, cast

from pydantic import ValidationError as PydanticValidationError
from pydantic_settings import BaseSettings, DotEnvSettingsSource, EnvSettingsSource
from typing_extensions import Self

from .declaration import _MISSING, Declaration, VarSpec, declaration
from .errors import ConfigValidationError, ErrorCode, Violation, write_termination_log
from .files import FileResult, load_file

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
        "too_short",
        "too_long",
    }
)


class Env:
    """The environment as pydantic-settings sees it (case folding, opt-in .env file)."""

    def __init__(self, cls: type[BaseSettings], decl: Declaration) -> None:
        self.case_sensitive = decl.case_sensitive
        values: dict[str, str | None] = {}
        if decl.env_file:
            # pydantic-settings' own dotenv reader; real environment variables win.
            values.update(DotEnvSettingsSource(cls).env_vars)
        values.update(EnvSettingsSource(cls).env_vars)
        self.values = values

    def __call__(self, name: str) -> str | None:
        return self.values.get(name if self.case_sensitive else name.lower())

    def first(self, names: tuple[str, ...]) -> str | None:
        for n in names:
            v = self(n)
            if v is not None:
                return v
        return None


def _set_path(d: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    for k in path[:-1]:
        d = d.setdefault(k, {})
    d[path[-1]] = value


#: Reference schemes of injectors that resolve env values when the process starts (SPEC §4.5.1).
INJECTOR_SCHEMES = ("vault:", "op://", "ref+")


def unresolved_reference(raw: str) -> str | None:
    """The injector scheme ``raw`` starts with, if it is still an unresolved reference."""
    return next((s for s in INJECTOR_SCHEMES if raw.startswith(s)), None)


def _precheck(v: VarSpec, raw: str, init: dict[str, Any], out: list[Violation]) -> bool:
    """SPEC §5 rules the host does not apply. Returns False if the var is handled."""
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
            out.append(Violation(v.name, "var", "invalid_type", "not a 64-bit signed integer"))
            return False
    if v.type == "float":
        try:
            f = float(raw)
        except ValueError:
            return True
        if not math.isfinite(f):
            out.append(Violation(v.name, "var", "invalid_type", "must be a finite number"))
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


def _violations_from(decl: Declaration, e: PydanticValidationError, skip: set[str]) -> list[Violation]:
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
            out.append(Violation(v.name, "var", code, _message(v, err, nested)))
        else:
            # A model validator, or a field docuconf does not export (Exclude).
            name = ".".join(loc) or decl.settings_cls.__name__
            out.append(Violation(name, "model", _code(None, err, False), _message(None, err, loc[1:])))
    return out


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
    floats), reads and checks every file input, and raises one
    :class:`ConfigValidationError` listing every violation with its SPEC code.
    The message is also written to ``/dev/termination-log`` when it exists (or
    to ``DOCUCONF_TERMINATION_LOG``); pass ``termination_log=False`` to skip.

    File inputs declared with ``reload="watch"`` are polled every
    ``watch_interval`` seconds (see :func:`get_watcher`). Extra keyword
    arguments are passed to the settings constructor (they win over the
    environment, as in pydantic-settings).
    """
    decl = declaration(cls)
    for w in decl.warnings:
        log.debug("docuconf: %s", w)
    env = Env(cls, decl)
    violations: list[Violation] = []
    kwargs: dict[str, Any] = {}
    skip: set[str] = set()

    for v in decl.vars:
        raw = env.first(v.env_names)
        if raw is None:
            continue
        if "deprecated" in v.common:
            dep = v.common["deprecated"]
            repl = f"; use {dep['replacedBy']}" if "replacedBy" in dep else ""
            log.warning("docuconf: %s is deprecated: %s%s", v.name, dep["message"], repl)
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
        if not _precheck(v, raw, kwargs, violations) and violations and violations[-1].input == v.name:
            skip.add(v.name)

    results: list[FileResult] = []
    for f in decl.files:
        r = load_file(f, env, now=now or datetime.now(timezone.utc))
        results.append(r)
        violations.extend(r.violations)
        skip.add(f.field_name)
        skip.add(f.init_key)
        if r.value is not None:
            kwargs[f.init_key] = r.value
        elif f.py_default is not _MISSING:
            kwargs[f.init_key] = f.py_default

    kwargs.update(init)
    settings: S | None = None
    try:
        settings = cls(**kwargs)
    except PydanticValidationError as e:
        violations.extend(_violations_from(decl, e, skip))

    if violations or settings is None:
        violations.sort(key=lambda v: (v.kind == "file", v.input))
        err = ConfigValidationError(violations)
        if termination_log is not False:
            write_termination_log(str(err), termination_log if isinstance(termination_log, str) else None)
        raise err

    if watch and any(f.marker.reload == "watch" for f in decl.files):
        from .watch import Watcher

        Watcher.start(settings, decl, env, results, interval=watch_interval)
    return settings


class DocuconfSettings:
    """Mixin adding ``Settings.load()``::

    class Settings(DocuconfSettings, BaseSettings): ...
    settings = Settings.load()
    """

    @classmethod
    def load(cls, **kwargs: Any) -> Self:
        if not issubclass(cls, BaseSettings):
            raise TypeError("DocuconfSettings must be mixed into a pydantic-settings BaseSettings class")
        return cast(Self, load(cls, **kwargs))
