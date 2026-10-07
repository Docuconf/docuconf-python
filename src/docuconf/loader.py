"""Boot-time loading: pydantic-settings parses, docuconf checks (SPEC §11.2)."""

from __future__ import annotations

import functools
import json
import logging
import math
import os
import re
import sys
import warnings
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, TypeVar, cast
from urllib.parse import unquote, urlsplit

from pydantic import ValidationError as PydanticValidationError
from pydantic._internal._model_construction import ModelMetaclass
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsError,
)
from typing_extensions import Self

from . import _context
from .declaration import _MISSING, REDACTING_TYPES, Declaration, VarSpec, declaration
from .errors import (
    ConfigValidationError,
    DeclarationError,
    DocuconfError,
    DocuconfWarning,
    ErrorCode,
    Violation,
    write_termination_log,
)
from .files import FileResult, load_file
from .overlays import is_wired, lenient, read_overlay, with_overlays
from .values import _FileValue

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


class _Unset:
    pass


_UNSET: Any = _Unset()


class Env:
    """The environment as pydantic-settings sees it (case folding, opt-in .env file).

    With ``values``, the environment is that mapping alone: docuconf reads
    nothing from ``os.environ`` or a ``.env`` file. ``file_root`` is the
    directory prepended to absolute file paths; by default it is
    ``DOCUCONF_FILE_ROOT`` from the same environment.
    """

    def __init__(
        self,
        cls: type[BaseSettings],
        decl: Declaration,
        values: Mapping[str, str] | None = None,
        file_root: str | None = _UNSET,
    ) -> None:
        self.case_sensitive = decl.case_sensitive
        self.own = values is not None
        if values is not None:
            bad = [k for k, v in values.items() if not isinstance(k, str) or not isinstance(v, str)]
            if bad:
                raise TypeError(f"env= must map names to strings, as the environment does; not {bad[0]!r}")
            #: Names as set, before case folding.
            self.names: list[str] = list(values)
            self.values: dict[str, str | None] = {
                (k if self.case_sensitive else k.lower()): v for k, v in values.items()
            }
            root = values.get("DOCUCONF_FILE_ROOT")
        else:
            loaded: dict[str, str | None] = {}
            names: list[str] = []
            if decl.env_file:
                # pydantic-settings' own dotenv reader; real environment variables win.
                dotenv = DotEnvSettingsSource(cls).env_vars
                loaded.update(dotenv)
                names.extend(dotenv)
            loaded.update(EnvSettingsSource(cls).env_vars)
            names.extend(os.environ)
            self.names = names
            self.values = loaded
            root = os.environ.get("DOCUCONF_FILE_ROOT")
        self.file_root: str | None = (root or None) if isinstance(file_root, _Unset) else (file_root or None)

    def visible(self, hidden: set[str]) -> dict[str, str | None]:
        """The values pydantic-settings may read: all but ``hidden`` (which docuconf reports or reads itself)."""
        fold = (lambda n: n) if self.case_sensitive else str.lower
        wanted = {fold(n) for n in hidden}
        return {k: v for k, v in self.values.items() if fold(k) not in wanted}

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


class ActiveEnvSource(EnvSettingsSource):
    """pydantic-settings' environment source, reading the environment docuconf loaded instead of ``os.environ``.

    docuconf installs it in ``settings_customise_sources`` while it builds a
    settings object, so ``load(..., env=...)`` never touches the process
    environment, and docuconf can keep a variable it reports itself (malformed
    JSON, say) away from pydantic-settings without deleting it from ``os.environ``.
    """

    def _load_env_vars(self) -> Mapping[str, str | None]:
        active = _context.active()
        if active is None:
            return super()._load_env_vars()
        return dict(active.values)


def swap_sources(
    settings_cls: type[BaseSettings],
    env_settings: PydanticBaseSettingsSource,
    dotenv_settings: PydanticBaseSettingsSource,
) -> tuple[PydanticBaseSettingsSource, PydanticBaseSettingsSource]:
    """While docuconf builds ``settings_cls``: its own environment source, and no separate .env source.

    docuconf's environment already holds the .env values (real variables win), as pydantic-settings layers them.
    """
    if _context.active() is None:
        return env_settings, dotenv_settings
    return ActiveEnvSource(settings_cls), InitSettingsSource(settings_cls, init_kwargs={})


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
    if v is not None and v.type in ("string", "url") and t in ("too_short", "too_long"):
        return "out_of_range"  # the length of a SecretStr, which pydantic reports like a list's
    if v is not None and v.type == "json" and nested and t != "missing":
        return "schema_mismatch"
    if t in _RANGE:
        return "out_of_range"
    return _CODES.get(t, "invalid_type")


#: An example value per duration encoding, for the error message (SPEC §5).
DURATION_EXAMPLES = {
    "iso8601": ("an ISO 8601 duration", "PT30S"),
    "go": ("a Go duration", "30s"),
    "seconds": ("a number of seconds", "30"),
    "timespan": ("a .NET TimeSpan", "00:00:30"),
}
_GO_DURATION = re.compile(r"^[0-9.]+(ns|us|µs|ms|s|m|h)([0-9.]+(ns|us|µs|ms|s|m|h))*$")


def _show(raw: str) -> str:
    return repr(raw if len(raw) <= 60 else raw[:57] + "...")


def _message(v: VarSpec | None, err: Mapping[str, Any], nested_loc: tuple[Any, ...], raw: str | None = None) -> str:
    t = err["type"]
    if t == "missing":
        return "required, but not set"
    prefix = ".".join(str(p) for p in nested_loc)
    prefix = f"{prefix}: " if prefix else ""
    secret = v is not None and v.secret
    if v is not None and v.type == "duration" and not nested_loc and t not in _RANGE:
        # pydantic's own message ("day" identifier in duration not correctly formatted) does not say what to write.
        encoding = v.attrs.get("encoding", "iso8601")
        what, example = DURATION_EXAMPLES.get(encoding, ("a duration", "30s"))
        msg = f"expected {what} like {example}"
        if not secret and raw is not None:
            msg += f" (got {_show(raw)})"
            if encoding == "iso8601" and _GO_DURATION.match(raw):
                msg += '; to accept values like 30s, use Annotated[timedelta, docuconf.Duration("go")]'
        return msg
    if v is not None and v.type in ("string", "url") and t in ("too_short", "too_long") and not nested_loc:
        ctx = err.get("ctx") or {}
        if t == "too_short":
            return f"should have at least {ctx.get('min_length')} characters"
        return f"should have at most {ctx.get('max_length')} characters"
    if secret and t not in _SAFE_FOR_SECRETS:
        return f"{prefix}invalid value (secret, not shown)"
    msg = f"{prefix}{err['msg']}"
    inp = err.get("input")
    if not secret and not nested_loc:
        if isinstance(inp, str):
            msg += f" (got {_show(inp)})"
        elif raw is not None:
            # The value as set, where pydantic reports the parsed one (a timedelta, say).
            msg += f" (got {_show(raw)})"
    return msg


def _violations_from(
    decl: Declaration,
    e: PydanticValidationError,
    skip: set[str],
    origin: Mapping[str, str],
    raws: Mapping[str, str] | None = None,
) -> list[Violation]:
    raws = raws or {}
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
            msg = _message(v, err, nested, raws.get(v.name))
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
            # A model validator, or a field docuconf does not export (Exclude). Its message is the
            # app's own text, which may quote a secret; scrub_secrets() removes those values.
            name = ".".join(loc) or decl.settings_cls.__name__
            out.append(Violation(name, "model", _code(None, err, False), _message(None, err, loc[1:])))
    return out


REDACTED = "**********"


def scrub_secrets(violations: list[Violation], secrets: list[str]) -> list[Violation]:
    """Replace every secret value in the messages: user-written validators can quote them (SPEC §6).

    Values of 4 characters or more are replaced wherever they appear; shorter
    ones only as a whole word, so a secret such as ``42`` does not mangle
    unrelated numbers.
    """
    secrets = sorted({s for s in secrets if s}, key=len, reverse=True)
    if not secrets:
        return violations
    out: list[Violation] = []
    for v in violations:
        msg = v.message
        for sec in secrets:
            if len(sec) >= 4:
                msg = msg.replace(sec, REDACTED)
            else:
                msg = re.sub(rf"(?<!\w){re.escape(sec)}(?!\w)", REDACTED, msg)
        out.append(v if msg == v.message else Violation(v.input, v.kind, v.code, msg))
    return out


def _url_secrets(raw: str) -> list[str]:
    """The credentials inside a URL-shaped secret, which a message may quote on their own."""
    if "://" not in raw:
        return []
    try:
        parts = urlsplit(raw)
        password = parts.password
    except ValueError:
        return []
    out = [p for p in (password, unquote(password) if password else None) if p]
    if parts.username and password:
        out.append(f"{parts.username}:{password}")
    return out


def secret_values(decl: Declaration, env: Env, files: Mapping[str, Any] | None = None) -> list[str]:
    """Every secret value docuconf read: secret variables as set, and the contents of secret text files."""
    out: list[str] = []
    for v in decl.vars:
        if v.secret:
            raw = env.first(v.env_names)
            if raw:
                out.append(raw)
                out.extend(_url_secrets(raw))
    for f in decl.files:
        value = (files or {}).get(f.init_key)
        if f.secret and isinstance(value, str):
            out.append(value)
            out.extend(line for line in value.splitlines() if len(line) >= 4)
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
    into: S | None = None,
) -> tuple[S | None, list[Violation]]:
    """Check the variables and overlays, then instantiate ``cls``; file inputs are already loaded.

    With ``into``, a ``DocuconfSettings`` object being initialised (``Settings()``), that object is filled in.
    """
    violations: list[Violation] = []
    kwargs: dict[str, Any] = {}
    skip = set(file_fields)
    hide: set[str] = set()

    overlay_data: list[tuple[str, dict[str, Any]]] = []
    for o in decl.overlays:
        data, bad = read_overlay(cls, o, env.file_root)
        if bad is not None:
            violations.append(bad)
        overlay_data.append((o.name, data))

    origin: dict[str, str] = {}
    indexed: dict[str, list[str]] = {}
    raws: dict[str, str] = {}
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
        if raw is not None and not v.secret:
            raws[v.name] = raw
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
        with lenient(), _context.activate(env.visible(hide), env.file_root):
            settings = _construct(cls, kwargs, into)
    except PydanticValidationError as e:
        violations.extend(_violations_from(decl, e, skip, origin, raws))
    except SettingsError as e:
        # A value pydantic-settings could not decode that docuconf did not hide (from a .env file, say).
        if not violations:
            violations.append(Violation(cls.__name__, "model", "invalid_type", _first_line(e)))
    if decl.overlays and not is_wired(cls):
        raise DeclarationError(
            [
                f"{cls.__name__} declares overlays, but settings_customise_sources does not load them: return "
                "docuconf.with_overlays(settings_cls, init_settings, env_settings, dotenv_settings, "
                "file_secret_settings, ...), or keep DocuconfSettings' own settings_customise_sources"
            ]
        )
    return settings, scrub_secrets(violations, secret_values(decl, env, file_kwargs))


def _construct(cls: type[S], kwargs: dict[str, Any], into: S | None) -> S:
    """Run pydantic-settings for ``cls`` (skipping DocuconfSettings.__init__, which is what called docuconf)."""
    if into is not None:
        super(DocuconfSettings, cast(DocuconfSettings, into)).__init__(**kwargs)
        return into
    if issubclass(cls, DocuconfSettings):
        obj = cls.__new__(cls)
        super(DocuconfSettings, obj).__init__(**kwargs)
        return obj
    return cls(**kwargs)


def _warn(message: str) -> None:
    """``warnings.warn`` a :class:`DocuconfWarning`, attributed to the first caller outside docuconf."""
    here = os.path.dirname(os.path.abspath(__file__))
    level = 2
    frame = sys._getframe(1)
    while frame is not None and os.path.dirname(os.path.abspath(frame.f_code.co_filename)) == here:
        frame = frame.f_back  # type: ignore[assignment]
        level += 1
    warnings.warn(message, DocuconfWarning, stacklevel=level)


def _distance(a: str, b: str) -> int:
    """Edit distance with adjacent transpositions (``PROT`` to ``PORT`` is 1)."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def typo_hints(decl: Declaration, env: Env) -> list[str]:
    """Set variables that are not declared but look like a typo of one that is (never their values).

    Only names after the class's ``env_prefix`` are compared, and only names of
    at least 4 characters after it. Names of up to 5 characters allow one edit
    (``PROT`` for ``PORT``), longer ones two, so ``HOST`` is not taken for ``PORT``.
    """
    fold = (lambda n: n) if decl.case_sensitive else str.upper
    prefix = fold(decl.settings_cls.model_config.get("env_prefix", "") or "")
    declared = {fold(n) for v in decl.vars for n in v.env_names}
    declared |= {fold(f.marker.path_env) for f in decl.files if f.marker.path_env}
    item_prefixes = tuple(
        fold(n) + "__" for v in decl.vars if v.attrs.get("encoding") == "indexed" for n in v.env_names
    )
    candidates = sorted(d for d in declared if d.startswith(prefix))
    out: list[str] = []
    for set_name in sorted(set(env.names)):
        name = fold(set_name)
        if name in declared or name.startswith("DOCUCONF_") or (item_prefixes and name.startswith(item_prefixes)):
            continue
        if not name.startswith(prefix):
            continue
        rest = name[len(prefix) :]
        best: tuple[int, str] | None = None
        for d in candidates:
            want = d[len(prefix) :]
            if min(len(rest), len(want)) < 4:
                continue  # too short to tell a typo from another name (PWD and PW)
            limit = 1 if min(len(rest), len(want)) <= 5 else 2
            dist = _distance(rest, want)
            if 0 < dist <= limit and (best is None or dist < best[0]):
                best = (dist, d)
        if best is not None:
            out.append(f"docuconf: {set_name} is set but not declared; did you mean {best[1]}?")
    return out


def load(
    cls: type[S],
    *,
    env: Mapping[str, str] | None = None,
    file_root: str | None = _UNSET,
    watch: bool | None = None,
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

    ``env`` replaces the process environment with a mapping, for tests:
    nothing is read from ``os.environ`` or a ``.env`` file, and ``file_root``
    (default: ``env["DOCUCONF_FILE_ROOT"]``) is prepended to absolute file
    paths. With ``env``, ``watch`` and ``termination_log`` default to off.
    docuconf never changes ``os.environ``.

    File inputs and overlays declared with ``reload="watch"`` are polled every
    ``watch_interval`` seconds (see :func:`get_watcher`) until the settings
    object is garbage-collected or the watcher is stopped. Extra keyword
    arguments are passed to the settings constructor (they win over the
    environment, as in pydantic-settings).

    For a :class:`DocuconfSettings` class, ``Settings()`` is ``load(Settings)``.
    """
    return _load(
        cls,
        None,
        env=env,
        file_root=file_root,
        watch=watch,
        watch_interval=watch_interval,
        termination_log=termination_log,
        now=now,
        init=init,
    )


def load_or_exit(cls: type[S], **kwargs: Any) -> S:
    """:func:`load`, or print the problems to stderr and exit with status 1, without a traceback.

    The one-liner for a service's entry point (and a Django ``settings.py``)::

        settings = docuconf.load_or_exit(Settings)   # or Settings.load_or_exit()

    The output is the ``docuconf: N configuration problems:`` block, one line
    per problem; the same text goes to the termination log.
    """
    try:
        return load(cls, **kwargs)
    except ConfigValidationError as e:
        print(e, file=sys.stderr)
    except DeclarationError as e:
        print(e, file=sys.stderr)
        log_target = kwargs.get("termination_log")
        if log_target is not False and kwargs.get("env") is None:
            write_termination_log(str(e), log_target if isinstance(log_target, str) else None)
    raise SystemExit(1)


def _adopt(into: BaseSettings, other: BaseSettings) -> None:
    for attr in ("__dict__", "__pydantic_fields_set__", "__pydantic_extra__", "__pydantic_private__"):
        object.__setattr__(into, attr, getattr(other, attr))


def _load(
    cls: type[S],
    into: S | None,
    *,
    env: Mapping[str, str] | None,
    file_root: str | None,
    watch: bool | None,
    watch_interval: float,
    termination_log: str | bool | None,
    now: datetime | None,
    init: Mapping[str, Any],
) -> S:
    if not isinstance(cls, type):
        raise TypeError(
            f"docuconf.load() takes the settings class, not an instance: load({type(cls).__name__}), "
            f"not load({type(cls).__name__}())"
        )
    if not issubclass(cls, BaseSettings):
        raise TypeError(f"{cls.__name__} is not a pydantic-settings class; subclass docuconf.DocuconfSettings")
    if _context.exporting.get():
        # docuconf export imports the module; a module-level load() must not validate an environment
        # that is not there. The export only reads the class.
        built = cast(S, cls.model_construct(**init))
        if into is None:
            return built
        _adopt(into, built)
        return into
    decl = declaration(cls)
    for w in decl.warnings:
        _warn(f"docuconf: {w}")
    environment = Env(cls, decl, env, file_root)
    if watch is None:
        watch = env is None
    if termination_log is None and env is not None:
        termination_log = False
    for hint in typo_hints(decl, environment):
        _warn(hint)
    violations: list[Violation] = []
    file_kwargs: dict[str, Any] = {}
    file_fields: set[str] = set()

    results: list[FileResult] = []
    for f in decl.files:
        r = load_file(f, environment, now=now or datetime.now(timezone.utc), root=environment.file_root)
        results.append(r)
        violations.extend(r.violations)
        file_fields.add(f.field_name)
        file_fields.add(f.init_key)
        if r.value is not None:
            file_kwargs[f.init_key] = r.value
        elif f.py_default is not _MISSING:
            file_kwargs[f.init_key] = f.py_default

    settings, more = build(cls, decl, environment, file_kwargs, file_fields, init, into=into)
    violations.extend(more)

    if violations or settings is None:
        violations = scrub_secrets(violations, secret_values(decl, environment, file_kwargs))
        violations.sort(key=lambda v: (v.kind == "file", v.input))
        err = ConfigValidationError(violations)
        if termination_log is not False:
            write_termination_log(str(err), termination_log if isinstance(termination_log, str) else None)
        raise err

    if watch and (
        any(f.marker.reload == "watch" for f in decl.files) or any(o.reload == "watch" for o in decl.overlays)
    ):
        from .watch import Watcher

        Watcher.start(settings, decl, environment, results, init=init, interval=watch_interval)
    return settings


class _RedactSecrets:
    """``repr()`` and ``str()`` show secret fields as ``'**********'``, whatever their type (SPEC §6)."""

    def __repr_args__(self) -> Iterator[tuple[str | None, Any]]:
        try:
            secret = declaration(cast("type[BaseSettings]", type(self))).secret_fields
        except DocuconfError:
            secret = frozenset()
        for name, value in super().__repr_args__():  # type: ignore[misc]
            if name in secret and value is not None and not isinstance(value, _SELF_REDACTING):
                value = REDACTED
            yield name, value


#: Values whose repr() never shows a secret: pydantic's secret types, docuconf's file values, paths.
_SELF_REDACTING: tuple[type, ...] = (*REDACTING_TYPES, _FileValue, Path)


class _DocuconfMeta(ModelMetaclass):
    """Reports ``class Settings(BaseSettings, DocuconfSettings)``, which Python would reject with an MRO error."""

    # __prepare__ rather than __new__: pydantic's __new__ reads the namespace of the frame that defines the
    # class, so it must be called from there directly.
    @classmethod
    def __prepare__(mcs, *args: Any, **kwargs: Any) -> dict[str, object]:
        name, bases = args[0], args[1]
        for i, base in enumerate(bases):
            if base is BaseSettings and any(getattr(b, "__docuconf_managed__", False) for b in bases[i + 1 :]):
                raise TypeError(
                    f"class {name}: put DocuconfSettings before BaseSettings in the bases, or drop BaseSettings: "
                    f"class {name}(DocuconfSettings): ..."
                )
        return super().__prepare__(*args, **kwargs)


class DocuconfSettings(_RedactSecrets, BaseSettings, metaclass=_DocuconfMeta):
    """Base class for docuconf settings: a pydantic-settings ``BaseSettings`` whose constructor is :func:`load`::

        class Settings(DocuconfSettings):
            port: int = Field(8080, description="HTTP listen port")

        settings = Settings()                # the same as docuconf.load(Settings)
        settings = Settings.load_or_exit()   # at a service's entry point
        settings = Settings.load(env={...})  # in tests

    ``Settings()`` runs every docuconf check and reads the file inputs, so it
    behaves exactly like ``docuconf.load(Settings)``: it raises
    :class:`ConfigValidationError` (never a raw pydantic ``ValidationError``),
    writes the termination log and starts the watcher. ``repr()`` hides every
    secret field. Its ``settings_customise_sources`` keeps pydantic-settings'
    default sources and adds the declared overlays (see
    :func:`docuconf.with_overlays`); override it to add baked-in config files.
    """

    __docuconf_managed__: ClassVar[bool] = True

    def __init__(self, **values: Any) -> None:
        _load(
            type(self),
            self,
            env=None,
            file_root=_UNSET,
            watch=None,
            watch_interval=2.0,
            termination_log=None,
            now=None,
            init=values,
        )

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        own = cls.__dict__.get("settings_customise_sources")
        func = getattr(own, "__func__", None)
        if func is None or getattr(func, "_docuconf_swaps", False):
            return

        # An override of settings_customise_sources: hand it docuconf's environment source.
        @functools.wraps(func)
        def wrapped(
            klass: type[BaseSettings],
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            env_settings, dotenv_settings = swap_sources(settings_cls, env_settings, dotenv_settings)
            result: tuple[PydanticBaseSettingsSource, ...] = func(
                klass, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
            )
            return result

        wrapped._docuconf_swaps = True  # type: ignore[attr-defined]
        type.__setattr__(cls, "settings_customise_sources", classmethod(wrapped))

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        env_settings, dotenv_settings = swap_sources(settings_cls, env_settings, dotenv_settings)
        return with_overlays(settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings)

    @classmethod
    def load(cls, **kwargs: Any) -> Self:
        """:func:`docuconf.load` for this class; takes the same arguments (``env=``, ``file_root=``...)."""
        return load(cls, **kwargs)

    @classmethod
    def load_or_exit(cls, **kwargs: Any) -> Self:
        """:func:`docuconf.load_or_exit` for this class."""
        return load_or_exit(cls, **kwargs)
