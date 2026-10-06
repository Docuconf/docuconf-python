"""Read a pydantic-settings class into a docuconf declaration (SPEC §4).

The settings class stays the single source of truth: env names follow
pydantic-settings' own rules (``env_prefix``, aliases,
``env_nested_delimiter``), constraints come from ``Field`` and
``annotated_types`` metadata, and docuconf only adds what pydantic cannot
express (see ``docuconf.markers``).
"""

from __future__ import annotations

import enum
import posixpath
import re
import types
import typing
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Union, get_args, get_origin

import pydantic_core
from pydantic import AliasChoices, BaseModel, ConfigDict, SecretStr, TypeAdapter, UrlConstraints
from pydantic import ValidationError as PydanticValidationError
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, NoDecode

from . import durations
from .errors import DeclarationError
from .markers import (
    FILE_MARKERS,
    BinaryFile,
    CaBundleFile,
    ConfigFile,
    Csv,
    Exclude,
    FileInput,
    KeystoreFile,
    Meta,
    Overlay,
    Secret,
    TextFile,
    TlsFile,
    Url,
)
from .re2 import non_re2_feature
from .values import CaBundle, Keystore, TlsKeyPair

# pydantic < 2.10 defines its URL types as Annotated[pydantic_core.Url, UrlConstraints(...)];
# later versions as subclasses of the private _BaseUrl and _BaseMultiHostUrl.
_URL_TYPES: tuple[type, ...] = (pydantic_core.Url, pydantic_core.MultiHostUrl)
try:
    from pydantic.networks import _BaseMultiHostUrl, _BaseUrl

    _URL_TYPES += (_BaseUrl, _BaseMultiHostUrl)
except ImportError:  # pragma: no cover
    pass

ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
INPUT_NAME = re.compile(r"^[a-z]([-a-z0-9]{0,40}[a-z0-9])?$")
SERVICE_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
ABS_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")
FileFormat = Literal["json", "yaml", "toml"]
FORMAT_BY_SUFFIX: dict[str, FileFormat] = {".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml"}
#: Overlay keys nest at most this deep (SPEC §4.7, #MaxKeyDepth).
MAX_KEY_DEPTH = 8
FEATURE_FLAG = re.compile(r"^(FF|FEATURE|FEATURE_FLAG|ENABLE)_")

VarType = Literal["string", "int", "float", "bool", "duration", "url", "enum", "list", "json"]
FileType = Literal["config", "tls", "caBundle", "keystore", "text", "binary"]

_MISSING: Any = object()


@dataclass
class VarSpec:
    """One environment variable, as the contract and the loader see it."""

    name: str
    type: VarType
    description: str
    required: bool
    secret: bool
    #: Contract fields after the common ones, in output order (without default).
    attrs: dict[str, Any]
    #: Common optional fields: group, examples, deprecated, configKey.
    common: dict[str, Any]
    #: Contract form of the default, or _MISSING.
    default: Any
    #: Python default, used when an empty value means "unset" (SPEC §5).
    py_default: Any
    #: Path of field names in the settings model, as in pydantic error locations.
    loc: tuple[str, ...]
    #: The same path with aliases, as used for init kwargs and by-alias error locations.
    alias_loc: tuple[str, ...]
    #: Names pydantic-settings reads this variable from (before case folding).
    env_names: tuple[str, ...]

    def contract(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, "description": self.description}
        if self.required:
            out["required"] = True
        if self.secret:
            out["secret"] = True
        out.update(self.common)
        out.update(self.attrs)
        if self.default is not _MISSING:
            out["default"] = self.default
        return out


@dataclass
class FileSpec:
    """One file input."""

    name: str
    type: FileType
    marker: FileInput
    field_name: str
    init_key: str
    description: str
    required: bool
    secret: bool
    #: The field's (unwrapped) type: the model a config file binds to, ``str``, ``bytes``, ``Path``...
    value_type: Any
    adapter: TypeAdapter[Any] | None
    py_default: Any
    contract_fields: dict[str, Any]
    min_remaining_ns: int | None = None

    def contract(self) -> dict[str, Any]:
        return dict(self.contract_fields)


@dataclass
class OverlaySpec:
    """One config-file overlay (SPEC §4.7)."""

    name: str
    format: FileFormat
    path: str
    reload: Literal["restart", "watch"]
    description: str | None
    marker: Overlay

    def contract(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.description:
            out["description"] = self.description
        out["format"] = self.format
        out["path"] = self.path
        out["keySeparator"] = OVERLAY_KEY_SEPARATOR
        if self.reload == "watch":
            out["reload"] = "watch"
        return out


#: pydantic-settings' config file sources nest values by field path; the contract joins it with ".".
OVERLAY_KEY_SEPARATOR = "."


def mount_dir(f: FileSpec) -> str:
    """The directory the platform mounts for a file input (SPEC §4.6)."""
    return f.marker.path if f.type == "tls" else posixpath.dirname(f.marker.path)


def bad_path(path: str) -> bool:
    return bool(not ABS_PATH.match(path) or re.search(r"(^|/)\.\.?(/|$)", path) or "//" in path or path.endswith("/"))


@dataclass
class Declaration:
    settings_cls: type[BaseSettings]
    service: str | None
    vars: list[VarSpec]
    files: list[FileSpec]
    warnings: list[str]
    case_sensitive: bool
    env_file: Any = None
    overlays: list[OverlaySpec] = field(default_factory=list)
    by_loc: dict[tuple[str, ...], VarSpec] = field(default_factory=dict)

    def var(self, name: str) -> VarSpec | None:
        return next((v for v in self.vars if v.name == name), None)


_cache: dict[type, Declaration] = {}


def declaration(cls: type[BaseSettings]) -> Declaration:
    """Read and check the declaration of ``cls``; raises :class:`DeclarationError`."""
    cached = _cache.get(cls)
    if cached is None:
        cached = _Builder(cls).build()
        _cache[cls] = cached
    return cached


def kebab(name: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name)
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def default_service_name(cls: type) -> str | None:
    explicit = getattr(cls, "docuconf_service", None)
    if isinstance(explicit, str):
        return explicit
    base = re.sub(r"(Settings|Config|Configuration)$", "", cls.__name__)
    return kebab(base) or None


def _first_line(e: Exception) -> str:
    return str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__


def _unwrap(annotation: Any) -> tuple[Any, list[Any], bool]:
    """Strip ``Annotated`` and ``Optional``; return (type, metadata, optional)."""
    meta: list[Any] = []
    optional = False
    while True:
        origin = get_origin(annotation)
        if origin is Annotated:
            args = get_args(annotation)
            annotation = args[0]
            for m in args[1:]:
                if isinstance(m, FieldInfo):
                    meta.extend(m.metadata)
                else:
                    meta.append(m)
            continue
        if origin is Union or origin is types.UnionType:
            members = [a for a in get_args(annotation) if a is not type(None)]
            if len(members) < len(get_args(annotation)):
                optional = True
            if len(members) == 1:
                annotation = members[0]
                continue
        return annotation, meta, optional


def _has(meta: Sequence[Any], kind: type) -> bool:
    return any(m is kind or isinstance(m, kind) for m in meta)


def _first(meta: Sequence[Any], kind: type[Any]) -> Any:
    return next((m for m in meta if isinstance(m, kind)), None)


def _attr(meta: Sequence[Any], name: str) -> Any:
    """The last metadata value for a constraint name (``ge``, ``pattern``...)."""
    found = None
    for m in meta:
        if isinstance(m, (UrlConstraints, FieldInfo)):
            continue
        v = getattr(m, name, None)
        if v is not None:
            found = v
    return found


def _int_bounds(meta: Sequence[Any]) -> tuple[int | None, int | None]:
    """Inclusive integer bounds from ``ge``/``gt``/``le``/``lt`` metadata."""
    lo, hi = _attr(meta, "ge"), _attr(meta, "le")
    if _attr(meta, "gt") is not None:
        lo = _attr(meta, "gt") + 1
    if _attr(meta, "lt") is not None:
        hi = _attr(meta, "lt") - 1
    return (None if lo is None else int(lo)), (None if hi is None else int(hi))


def _is_subclass(t: Any, base: type | tuple[type, ...]) -> bool:
    # On 3.10, list[str] passes isinstance(..., type) but not issubclass.
    return isinstance(t, type) and get_origin(t) is None and issubclass(t, base)


def _str_values(t: Any) -> list[str] | None:
    """Enum values for a ``Literal`` of strings or a ``str``-valued ``Enum``."""
    if get_origin(t) is Literal:
        vals = list(get_args(t))
    elif _is_subclass(t, enum.Enum):
        vals = [m.value for m in t]
    else:
        return None
    return [str(v) for v in vals] if all(isinstance(v, str) for v in vals) else None


def _is_complex(t: Any) -> bool:
    """Whether pydantic-settings decodes the value as JSON."""
    origin = get_origin(t) or t
    if _is_subclass(origin, (str, bytes)):
        return False
    if _is_subclass(origin, (BaseModel, typing.Mapping, typing.Sequence, tuple, set, frozenset, dict, list)):
        return True
    if get_origin(t) in (Union, types.UnionType):
        return any(_is_complex(_unwrap(a)[0]) for a in get_args(t))
    return hasattr(origin, "__pydantic_core_schema__") or hasattr(origin, "__dataclass_fields__")


class _Builder:
    def __init__(self, cls: type[BaseSettings]) -> None:
        self.cls = cls
        cfg = cls.model_config
        self.prefix: str = cfg.get("env_prefix", "") or ""
        self.case_sensitive: bool = bool(cfg.get("case_sensitive", False))
        self.delim: str | None = cfg.get("env_nested_delimiter") or None
        self.python_re = cfg.get("regex_engine") == "python-re"
        self.ta_config = ConfigDict(regex_engine=cfg.get("regex_engine", "rust-regex"))
        self.problems: list[str] = []
        self.warnings: list[str] = []
        self.vars: list[VarSpec] = []
        self.files: list[FileSpec] = []
        self.overlays: list[OverlaySpec] = []

    def build(self) -> Declaration:
        if self.python_re:
            self.warnings.append(
                'regex_engine="python-re": patterns are exported as RE2, but Python re semantics '
                "differ ($ also matches before a final newline, \\d matches non-ASCII digits)"
            )
        self.read_overlays()
        for fname, fi in self.cls.model_fields.items():
            self.field(fname, fi)
        self.check_cross()
        if self.problems:
            raise DeclarationError(self.problems)
        vars_sorted = sorted(self.vars, key=lambda v: v.name)
        decl = Declaration(
            settings_cls=self.cls,
            service=default_service_name(self.cls),
            vars=vars_sorted,
            files=sorted(self.files, key=lambda f: f.name),
            warnings=self.warnings,
            case_sensitive=self.case_sensitive,
            env_file=self.cls.model_config.get("env_file"),
            overlays=self.overlays,
        )
        for v in vars_sorted:
            decl.by_loc[v.loc] = v
            decl.by_loc[v.alias_loc] = v
        return decl

    # -- fields -----------------------------------------------------------

    def env_names(self, fname: str, fi: FieldInfo) -> tuple[list[str], str]:
        """Env names pydantic-settings reads, and the key init kwargs use."""
        names: list[str] = []
        for alias in (fi.validation_alias, fi.alias):
            if isinstance(alias, str):
                names.append(alias)
            elif isinstance(alias, AliasChoices):
                names.extend(c for c in alias.choices if isinstance(c, str))
        names = list(dict.fromkeys(names))
        if names:
            canonical = fi.alias if isinstance(fi.alias, str) else names[0]
            return names, canonical
        return [self.prefix + fname], fname

    def field(self, fname: str, fi: FieldInfo) -> None:
        ann, inner_meta, _ = _unwrap(fi.annotation)
        meta = list(fi.metadata) + inner_meta
        if _has(meta, Exclude):
            return
        marker = next((m for m in meta if isinstance(m, FILE_MARKERS)), None)
        names, init_key = self.env_names(fname, fi)
        if marker is not None:
            self.file(fname, init_key, fi, ann, meta, marker)
            return
        if len(names) > 1:
            self.warnings.append(f"{fname}: read from {', '.join(names)}; the contract uses {names[0]}")
        self.var(
            loc=(fname,),
            alias_loc=(init_key,),
            env=names[0],
            env_names=tuple(names),
            fi=fi,
            ann=ann,
            meta=meta,
            required=fi.is_required(),
            full_annotation=fi.annotation,
        )

    def var(
        self,
        *,
        loc: tuple[str, ...],
        alias_loc: tuple[str, ...],
        env: str,
        env_names: tuple[str, ...],
        fi: FieldInfo,
        ann: Any,
        meta: list[Any],
        required: bool,
        full_annotation: Any,
    ) -> None:
        name = env if self.case_sensitive else env.upper()
        where = ".".join(loc)

        # Nested model with env_nested_delimiter: one variable per field.
        if _is_subclass(ann, BaseModel) and self.delim and not _has(meta, Secret):
            for sub, sfi in ann.model_fields.items():
                sann, smeta, _ = _unwrap(sfi.annotation)
                smeta = list(sfi.metadata) + smeta
                if _has(smeta, Exclude):
                    continue
                skey = sfi.alias if isinstance(sfi.alias, str) else sub
                self.var(
                    loc=(*loc, sub),
                    alias_loc=(*alias_loc, skey),
                    env=env + self.delim + skey,
                    env_names=(env + self.delim + skey,),
                    fi=sfi,
                    ann=sann,
                    meta=smeta,
                    required=required and sfi.is_required(),
                    full_annotation=sfi.annotation,
                )
            return

        problems_before = len(self.problems)

        def problem(msg: str) -> None:
            self.problems.append(f"{name} ({where}): {msg}")

        if not ENV_NAME.match(name):
            problem(
                f"env name must match {ENV_NAME.pattern}" + (" (case_sensitive=True)" if self.case_sensitive else "")
            )
        description = (fi.description or "").strip()
        if len(description) < 5:
            problem('needs a description of at least 5 characters: Field(description="...")')
        if FEATURE_FLAG.match(name):
            self.warnings.append(
                f"{name}: looks like a feature flag; flags that change without a rollout belong in a "
                "flag service (SPEC §10)"
            )

        secret = _is_subclass(ann, SecretStr) or _has(meta, Secret)
        vtype, attrs = self.classify(name, ann, meta, problem)
        if vtype is None:
            return

        common: dict[str, Any] = {}
        m: Meta | None = _first(meta, Meta)
        if m and m.group:
            common["group"] = m.group
        if fi.examples:
            if secret:
                problem("a secret must not have examples")
            common["examples"] = [str(e.value if isinstance(e, enum.Enum) else e) for e in fi.examples]
        if fi.deprecated:
            dep: dict[str, Any] = {
                "message": fi.deprecated
                if isinstance(fi.deprecated, str)
                else getattr(fi.deprecated, "message", "deprecated")
            }
            if m and m.replaced_by:
                dep["replacedBy"] = m.replaced_by
            common["deprecated"] = dep
        if m and m.config_key:
            common["configKey"] = m.config_key
        if self.overlays and not secret:
            # Where pydantic-settings' config file sources read the value: the field path, by alias.
            key = OVERLAY_KEY_SEPARATOR.join(alias_loc)
            if m and m.config_key and m.config_key != key:
                problem(f"config_key {m.config_key!r} must be {key!r}, where an overlay file holds this value")
            elif any(OVERLAY_KEY_SEPARATOR in p for p in alias_loc):
                problem(f"an overlay cannot hold {key!r}: a key part contains {OVERLAY_KEY_SEPARATOR!r}")
            elif len(alias_loc) > MAX_KEY_DEPTH:
                problem(f"an overlay cannot hold {key!r}: more than {MAX_KEY_DEPTH} levels deep")
            else:
                common["configKey"] = key

        py_default = _MISSING
        if not fi.is_required():
            py_default = fi.get_default(call_default_factory=True)
        default = _MISSING
        if py_default is not _MISSING and py_default is not None and not required:
            if secret:
                problem("a secret must not have a default (SPEC §6)")
            else:
                parts = (full_annotation, *fi.metadata)
                target = Annotated[parts] if fi.metadata else full_annotation
                try:
                    TypeAdapter(target, config=self.ta_config).validate_python(py_default)
                except PydanticValidationError as e:
                    msgs = "; ".join(err["msg"] for err in e.errors())
                    problem(f"default {py_default!r} does not satisfy the field's constraints: {msgs}")
                except Exception as e:  # e.g. a pattern the regex engine rejects
                    problem(f"cannot check the default: {_first_line(e)}")
                default = self.contract_default(vtype, ann, py_default, problem)
        if len(self.problems) > problems_before:
            return
        self.vars.append(
            VarSpec(
                name=name,
                type=vtype,
                description=description,
                required=required,
                secret=secret,
                attrs=attrs,
                common=common,
                default=default,
                py_default=py_default,
                loc=loc,
                alias_loc=alias_loc,
                env_names=env_names,
            )
        )

    def check_pattern(self, pattern: str, problem: Any) -> None:
        bad = non_re2_feature(pattern)
        if bad:
            problem(f"pattern uses {bad}, which RE2 does not support")

    def classify(self, name: str, ann: Any, meta: list[Any], problem: Any) -> tuple[VarType | None, dict[str, Any]]:
        attrs: dict[str, Any] = {}
        url = _first(meta, Url)
        if url is not None or _is_subclass(ann, _URL_TYPES):
            schemes: list[str] = []
            if url is not None:
                schemes = list(url.schemes)
            else:
                cons = getattr(ann, "_constraints", None)
                if cons is not None and cons.allowed_schemes:
                    schemes = list(cons.allowed_schemes)
            uc = _first(meta, UrlConstraints)
            if uc is not None and uc.allowed_schemes:
                schemes = list(uc.allowed_schemes)
            if schemes:
                attrs["schemes"] = schemes
            return "url", attrs

        if _is_subclass(ann, bool):
            return "bool", attrs
        if _is_subclass(ann, int) and not _is_subclass(ann, enum.Enum):
            lo, hi = _int_bounds(meta)
            if lo is not None:
                attrs["min"] = lo
            if hi is not None:
                attrs["max"] = hi
            return "int", attrs
        if _is_subclass(ann, (float, Decimal)):
            self.bounds(name, meta, attrs, lambda x: float(x) if isinstance(x, Decimal) else x)
            return "float", attrs
        if _is_subclass(ann, timedelta):
            attrs["encoding"] = "iso8601"
            self.bounds(name, meta, attrs, lambda x: durations.to_go(x) if isinstance(x, timedelta) else x)
            return "duration", attrs
        values = _str_values(ann)
        if values is not None:
            attrs["values"] = values
            return "enum", attrs
        if get_origin(ann) is Literal or _is_subclass(ann, enum.Enum):
            problem("enum values must be strings (a Literal or Enum of str)")
            return None, attrs

        origin = get_origin(ann)
        if origin in (list, tuple, set, frozenset) or (
            origin is not None and _is_subclass(origin, typing.Sequence) and not _is_subclass(origin, str)
        ):
            args = [a for a in get_args(ann) if a is not Ellipsis]
            item, item_meta, _ = _unwrap(args[0]) if len(args) == 1 else (None, [], False)
            items = None
            if _is_subclass(item, bool):
                items = None
            elif _is_subclass(item, int) and not _is_subclass(item, enum.Enum):
                items = "int"
            elif _is_subclass(item, str) or _str_values(item) is not None:
                items = "string"
            if items is not None:
                attrs["items"] = items
                csv = _first(meta, Csv)
                if _has(meta, NoDecode):
                    attrs["encoding"] = "csv"
                    attrs["separator"] = csv.separator if csv else ","
                    if csv is None:
                        self.warnings.append(f"{name}: NoDecode without docuconf.Csv; assuming a comma-separated value")
                else:
                    if csv is not None:
                        problem("docuconf.Csv needs NoDecode too, or pydantic-settings decodes the value as JSON")
                    attrs["encoding"] = "json"
                lo, hi = _attr(meta, "min_length"), _attr(meta, "max_length")
                if lo is not None:
                    attrs["minItems"] = lo
                if hi is not None:
                    attrs["maxItems"] = hi
                if items == "int":
                    # Container-element constraints: list[Annotated[int, Field(ge=0)]] or list[conint(ge=0)].
                    lo, hi = _int_bounds(item_meta)
                    if lo is not None:
                        attrs["itemMin"] = lo
                    if hi is not None:
                        attrs["itemMax"] = hi
                return "list", attrs

        if _is_subclass(ann, (str, Path, SecretStr)) or not _is_complex(ann):
            if not _is_subclass(ann, (str, SecretStr, Path)):
                self.warnings.append(f"{name}: {getattr(ann, '__name__', ann)} is exported as a string")
            lo, hi, pat = _attr(meta, "min_length"), _attr(meta, "max_length"), _attr(meta, "pattern")
            if lo is not None:
                attrs["minLength"] = lo
            if hi is not None:
                attrs["maxLength"] = hi
            if pat is not None:
                pat = pat if isinstance(pat, str) else pat.pattern
                self.check_pattern(pat, problem)
                attrs["pattern"] = pat
            return "string", attrs

        # Anything else pydantic-settings decodes as JSON: a model, dict or list of objects.
        try:
            attrs["schema"] = TypeAdapter(ann).json_schema()
        except Exception as e:  # pydantic raises several error types for unsupported schemas
            problem(f"cannot generate a JSON Schema: {e}")
            return None, attrs
        return "json", attrs

    def bounds(self, name: str, meta: list[Any], attrs: dict[str, Any], conv: Any) -> None:
        lo, hi = _attr(meta, "ge"), _attr(meta, "le")
        if _attr(meta, "gt") is not None:
            lo = _attr(meta, "gt")
            self.warnings.append(f"{name}: gt is exported as an inclusive min; the boot check stays exclusive")
        if _attr(meta, "lt") is not None:
            hi = _attr(meta, "lt")
            self.warnings.append(f"{name}: lt is exported as an inclusive max; the boot check stays exclusive")
        if lo is not None:
            attrs["min"] = conv(lo)
        if hi is not None:
            attrs["max"] = conv(hi)

    def contract_default(self, vtype: str, ann: Any, value: Any, problem: Any) -> Any:
        if isinstance(value, enum.Enum):
            value = value.value
        if vtype == "duration":
            if not isinstance(value, timedelta) or value < timedelta(0):
                problem("a duration default must be a non-negative timedelta")
                return _MISSING
            return durations.to_go(value)
        if vtype == "list":
            return [v.value if isinstance(v, enum.Enum) else v for v in value]
        if vtype == "json":
            return TypeAdapter(ann).dump_python(value, mode="json")
        if vtype in ("string", "url"):
            return str(value)
        if vtype == "float":
            return float(value)
        return value

    # -- files ------------------------------------------------------------

    def file(self, fname: str, init_key: str, fi: FieldInfo, ann: Any, meta: list[Any], marker: FileInput) -> None:
        name = marker.name or fname.replace("_", "-")

        def problem(msg: str) -> None:
            self.problems.append(f"file {name} ({fname}): {msg}")

        if not INPUT_NAME.match(name):
            problem(f"input name must be a DNS label matching {INPUT_NAME.pattern}")
        description = (fi.description or "").strip()
        if len(description) < 5:
            problem('needs a description of at least 5 characters: Field(description="...")')
        path = marker.path
        if bad_path(path):
            problem(f"path {path!r} must be absolute and normalised")
        if marker.reload not in ("restart", "watch"):
            problem('reload must be "restart" or "watch"')
        if marker.path_env is not None and not ENV_NAME.match(marker.path_env):
            problem(f"pathEnv {marker.path_env!r} must match {ENV_NAME.pattern}")
        if marker.max_size is not None and marker.max_size <= 0:
            problem("max_size must be positive")
        required = marker.required if marker.required is not None else fi.is_required()
        py_default = _MISSING if fi.is_required() else fi.get_default(call_default_factory=True)

        ftype: FileType
        secret = _has(meta, Secret)
        specific: dict[str, Any] = {}
        fmt: str | None = None
        adapter: TypeAdapter[Any] | None = None
        min_ns: int | None = None
        expect: tuple[type, ...] | None = None
        if isinstance(marker, ConfigFile):
            ftype = "config"
            fmt = marker.format
            if fmt is None:
                suffix = Path(path).suffix.lower()
                fmt = {".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml"}.get(suffix)
                if fmt is None:
                    problem("cannot infer the format from the file extension; set format=")
            elif fmt not in ("json", "yaml", "toml"):
                problem('format must be "json", "yaml" or "toml"')
            try:
                adapter = TypeAdapter(ann)
                specific["schema"] = adapter.json_schema()
            except Exception as e:
                problem(f"cannot generate a JSON Schema for {ann!r}: {e}")
        elif isinstance(marker, TlsFile):
            ftype, secret, expect = "tls", True, (TlsKeyPair,)
            if marker.dns_names:
                specific["dnsNames"] = list(marker.dns_names)
            if marker.key_algorithms:
                bad = [a for a in marker.key_algorithms if a not in ("RSA", "ECDSA", "Ed25519")]
                if bad:
                    problem(f"unknown key algorithms {bad}; use RSA, ECDSA or Ed25519")
                specific["keyAlgorithms"] = list(marker.key_algorithms)
            if marker.min_remaining is not None:
                try:
                    mr = marker.min_remaining
                    min_ns = (
                        durations.timedelta_to_ns(mr) if isinstance(mr, timedelta) else durations.parse_go_duration(mr)
                    )
                    specific["minRemaining"] = durations.format_go_duration(min_ns)
                except ValueError as e:
                    problem(f"min_remaining: {e}")
            if marker.require_ca:
                specific["requireCA"] = True
        elif isinstance(marker, CaBundleFile):
            ftype, expect = "caBundle", (CaBundle,)
            if marker.min_certificates < 1:
                problem("min_certificates must be at least 1")
            if marker.min_certificates != 1:
                specific["minCertificates"] = marker.min_certificates
        elif isinstance(marker, KeystoreFile):
            ftype, secret, expect, fmt = "keystore", True, (Keystore,), marker.format
            if marker.format != "pkcs12":
                problem('only format="pkcs12" is supported by the Python SDK')
            if marker.password_var:
                specific["passwordVar"] = marker.password_var
        elif isinstance(marker, TextFile):
            ftype, expect = "text", (str, Path)
            if marker.pattern is not None:
                self.check_pattern(marker.pattern, problem)
                try:
                    from pydantic import StringConstraints

                    TypeAdapter(Annotated[str, StringConstraints(pattern=marker.pattern)])
                except Exception as e:
                    problem(f"invalid pattern: {e}")
                specific["pattern"] = marker.pattern
            if marker.min_length is not None:
                specific["minLength"] = marker.min_length
            if marker.max_length is not None:
                specific["maxLength"] = marker.max_length
        elif isinstance(marker, BinaryFile):
            ftype, expect = "binary", (bytes, Path)
        else:  # pragma: no cover
            problem(f"unknown file marker {marker!r}")
            return
        if expect is not None and not _is_subclass(ann, expect):
            problem(
                f"field type must be {' or '.join(t.__name__ for t in expect)}, not {getattr(ann, '__name__', ann)}"
            )

        out: dict[str, Any] = {"type": ftype}
        if fmt is not None:
            out["format"] = fmt
        out["description"] = description
        if required:
            out["required"] = True
        if secret:
            out["secret"] = True
        if marker.group:
            out["group"] = marker.group
        if fi.deprecated:
            out["deprecated"] = {
                "message": fi.deprecated
                if isinstance(fi.deprecated, str)
                else getattr(fi.deprecated, "message", "deprecated")
            }
        out["path"] = path
        if marker.path_env:
            out["pathEnv"] = marker.path_env
        if marker.reload == "watch":
            out["reload"] = "watch"
        if marker.max_size is not None:
            out["maxSize"] = marker.max_size
        out.update(specific)
        self.files.append(
            FileSpec(
                name=name,
                type=ftype,
                marker=marker,
                field_name=fname,
                init_key=init_key,
                description=description,
                required=required,
                secret=secret,
                value_type=ann,
                adapter=adapter,
                py_default=py_default,
                contract_fields=out,
                min_remaining_ns=min_ns,
            )
        )

    # -- overlays ---------------------------------------------------------

    def read_overlays(self) -> None:
        declared = getattr(self.cls, "docuconf_overlays", None) or ()
        if isinstance(declared, Overlay):
            declared = (declared,)
        names: set[str] = set()
        for o in declared:
            if not isinstance(o, Overlay):
                self.problems.append(f"docuconf_overlays: {o!r} is not a docuconf.Overlay")
                continue

            def problem(msg: str, name: str = o.name) -> None:
                self.problems.append(f"overlay {name}: {msg}")

            if not INPUT_NAME.match(o.name):
                problem(f"name must be a DNS label matching {INPUT_NAME.pattern}")
            if o.name in names:
                problem("declared twice")
            names.add(o.name)
            if bad_path(o.path):
                problem(f"path {o.path!r} must be absolute and normalised")
            fmt = o.format
            if fmt is None:
                suffix = Path(o.path).suffix.lower()
                fmt = FORMAT_BY_SUFFIX.get(suffix)
                if fmt is None:
                    problem("cannot infer the format from the file extension; set format=")
                    continue
            elif fmt not in ("json", "yaml", "toml"):
                problem('format must be "json", "yaml" or "toml"')
                continue
            if o.reload not in ("restart", "watch"):
                problem('reload must be "restart" or "watch"')
            description = o.description.strip() if o.description is not None else None
            if description is not None and len(description) < 5:
                problem("description must be at least 5 characters")
            self.overlays.append(OverlaySpec(o.name, fmt, o.path, o.reload, description, o))

    def check_cross(self) -> None:
        seen: dict[str, str] = {}
        for v in self.vars:
            if v.name in seen:
                self.problems.append(f"{v.name}: declared twice ({seen[v.name]} and {'.'.join(v.loc)})")
            seen[v.name] = ".".join(v.loc)
        names: set[str] = set()
        path_envs: set[str] = set()
        mounts: dict[str, str] = {}
        for f in self.files:
            mounts.setdefault(mount_dir(f), f"file {f.name}")
        for o in self.overlays:
            d = posixpath.dirname(o.path)
            if d in mounts:
                # The platform mounts the overlay's directory, which would hide the other input.
                self.problems.append(f"overlay {o.name}: directory {d} is also mounted for {mounts[d]}")
            mounts[d] = f"overlay {o.name}"
        for f in self.files:
            if f.name in names:
                self.problems.append(f"file {f.name}: declared twice")
            names.add(f.name)
            pe = f.marker.path_env
            if pe:
                if pe in seen:
                    self.problems.append(f"file {f.name}: pathEnv {pe} must not also be a declared variable")
                if pe in path_envs:
                    self.problems.append(f"file {f.name}: pathEnv {pe} is used by another file input")
                path_envs.add(pe)
            if isinstance(f.marker, KeystoreFile) and f.marker.password_var:
                pv = next((v for v in self.vars if v.name == f.marker.password_var), None)
                if pv is None or not pv.secret:
                    self.problems.append(
                        f"file {f.name}: password_var {f.marker.password_var} must name a declared secret variable"
                    )
