"""Contract-first mode (SPEC §11.2 item 11): validate an environment against a contract given as data.

There is no in-language declaration. docuconf turns the contract into a
pydantic-settings class whose fields carry the same constraints and markers a
hand-written declaration would, then loads it through the same code as
:func:`docuconf.load`, so both modes share every check.
"""

from __future__ import annotations

import json
import keyword
import os
import posixpath
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Optional

from pydantic import Field, create_model
from pydantic_settings import BaseSettings, NoDecode, PydanticBaseSettingsSource, SettingsConfigDict

from . import durations
from .declaration import FORMAT_BY_SUFFIX, Declaration, OverlaySpec, declaration
from .errors import ConfigValidationError, DeclarationError, Violation, write_termination_log
from .keyset import Keys, KeySet
from .loader import ActiveEnvSource, Env, Layer, _RedactSecrets, build, load_files, wire_value
from .markers import (
    BinaryFile,
    CaBundleFile,
    ConfigFile,
    Csv,
    Duration,
    IndexedList,
    JsonMaxLength,
    JsonValue,
    KeystoreFile,
    Meta,
    Overlay,
    Secret,
    TextFile,
    TlsFile,
    Url,
    schema_validator,
)
from .overlays import read_overlay
from .re2 import non_re2_feature
from .values import CaBundle, Keystore, TlsKeyPair

API_VERSION = "docuconf.dev/v1alpha1"
_VAR_TYPES = ("string", "int", "float", "bool", "duration", "url", "enum", "list", "keySet", "json")
_FILE_TYPES = ("config", "tls", "caBundle", "keystore", "text", "binary")


class ContractSettings(_RedactSecrets, BaseSettings):
    """Base of the settings classes :func:`load_contract` builds: one field per input.

    A variable's field is named as the variable (``values.PORT``); a file
    input's is its name with ``_`` for ``-`` (``values.serving_tls``).
    :meth:`value` takes the input's own name. ``repr()`` shows secret inputs
    as ``'**********'``.
    """

    model_config = SettingsConfigDict(case_sensitive=True, extra="ignore")
    __docuconf_managed__: ClassVar[bool] = True
    #: Input name (a variable or file input) to field name.
    docuconf_inputs: ClassVar[dict[str, str]] = {}

    def value(self, name: str) -> Any:
        """The typed value of the input ``name``: a variable (``PORT``) or a file input (``serving-tls``)."""
        field = type(self).docuconf_inputs.get(name)
        if field is None:
            raise KeyError(name)
        # Read the stored value, so a deprecated field does not warn on access.
        return self.__dict__[field]

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Reads the environment load_contract was given, never os.environ.
        return (init_settings, ActiveEnvSource(settings_cls, case_sensitive=True))


def _read(contract: Mapping[str, Any] | str | os.PathLike[str]) -> Mapping[str, Any]:
    if isinstance(contract, Mapping):
        return contract
    if isinstance(contract, str) and contract.lstrip().startswith("{"):
        data = json.loads(contract)
    else:
        data = json.loads(Path(contract).read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise DeclarationError(["the contract must be a JSON object"])
    return data


_UNSET: Any = object()


def _field(
    name: str, var: Mapping[str, Any], problems: list[str], profile_default: Any = _UNSET
) -> tuple[Any, Any] | None:
    """The annotation and ``Field`` a hand-written declaration would use for ``var``.

    ``profile_default`` is the selected profile's value (SPEC §4.4): it
    replaces the variable's default and satisfies ``required``.
    """
    vtype = var.get("type")
    if vtype not in _VAR_TYPES:
        problems.append(f"{name}: unknown type {vtype!r}")
        return None
    required = bool(var.get("required", False))
    secret = bool(var.get("secret", False))
    default = var.get("default", None)
    if profile_default is not _UNSET:
        default, required = profile_default, False
    inner: list[Any] = []  # metadata on the value type
    outer: list[Any] = []  # metadata on the field (pydantic-settings reads NoDecode there)
    cons: dict[str, Any] = {}
    validate_default = False
    base: Any
    if vtype == "string":
        base = str
        bad = non_re2_feature(var["pattern"]) if isinstance(var.get("pattern"), str) else None
        if bad:
            problems.append(f"{name}: pattern uses {bad}, which RE2 does not support")
            return None
        cons = {"min_length": var.get("minLength"), "max_length": var.get("maxLength"), "pattern": var.get("pattern")}
    elif vtype == "int":
        base = int
        cons = {"ge": var.get("min"), "le": var.get("max")}
    elif vtype == "float":
        base = float
        cons = {"ge": var.get("min"), "le": var.get("max")}
    elif vtype == "bool":
        base = bool
    elif vtype == "duration":
        base = timedelta
        encoding = var.get("encoding", "go")
        if encoding not in ("go", "iso8601", "seconds", "timespan"):
            problems.append(f"{name}: unknown duration encoding {encoding!r}")
            return None
        inner.append(Duration(encoding))
        try:
            cons = {k: _duration(var.get(f)) for k, f in (("ge", "min"), ("le", "max"))}
            default = _duration(default)
        except ValueError as e:
            problems.append(f"{name}: {e}")
            return None
    elif vtype == "url":
        base = str
        inner.append(Url(schemes=tuple(var.get("schemes", ()))))
        cons = {"max_length": var.get("maxLength")}
    elif vtype == "enum":
        values = var.get("values") or []
        if not values or not all(isinstance(v, str) for v in values):
            problems.append(f"{name}: an enum needs a non-empty list of string values")
            return None
        base = Literal[tuple(values)]
    elif vtype == "list":
        items = var.get("items", "string")
        if items not in ("string", "int"):
            problems.append(f"{name}: list items must be string or int, not {items!r}")
            return None
        item: Any = str
        lengths: dict[str, Any] = {
            k: var[f] for k, f in (("min_length", "itemMinLength"), ("max_length", "itemMaxLength")) if f in var
        }
        if lengths and items != "string":
            problems.append(f"{name}: itemMinLength and itemMaxLength apply only to lists of strings")
            return None
        if lengths:
            item = Annotated[str, Field(**lengths)]
        if items == "int":
            item = int
            bounds: dict[str, Any] = {"ge": var.get("itemMin"), "le": var.get("itemMax")}
            bounds = {k: v for k, v in bounds.items() if v is not None}
            if bounds:
                item = Annotated[int, Field(**bounds)]
        base = list[item]
        encoding = var.get("encoding", "csv")
        if encoding == "csv":
            inner.append(Csv(var.get("separator", ",")))
            outer.append(NoDecode)
        elif encoding == "indexed":
            inner.append(IndexedList())
        elif encoding != "json":
            problems.append(f"{name}: unknown list encoding {encoding!r}")
            return None
        cons = {"min_length": var.get("minItems"), "max_length": var.get("maxItems")}
    elif vtype == "keySet":
        if not secret:
            problems.append(f"{name}: a keySet is always secret")
            return None
        keys = Keys(
            min_keys=var.get("minKeys", 1),
            max_keys=var.get("maxKeys", 2),
            key_min_length=var.get("keyMinLength"),
            key_max_length=var.get("keyMaxLength"),
            encoding=var.get("encoding", "csv"),
            separator=var.get("separator", ","),
        )
        base = KeySet
        inner.append(keys)
        secret = False  # KeySet is secret by type
    else:  # json
        base = Any
        schema = var.get("schema")
        if schema:
            try:
                schema_validator(schema)
            except ImportError:
                pass  # JsonValue logs that the schema is not checked
            except Exception as e:  # jsonschema.SchemaError
                problems.append(f"{name}: invalid JSON Schema: {getattr(e, 'message', e)}")
                return None
        inner.append(JsonValue(schema or None))
        if var.get("maxLength") is not None:
            inner.append(JsonMaxLength(var["maxLength"]))
        outer.append(NoDecode)
        if default is not None:
            # JSON text, decoded like a value from the environment, so a string default stays a string.
            default = json.dumps(default)
            validate_default = True
    if secret:
        inner.append(Secret())
    cons = {k: v for k, v in cons.items() if v is not None}
    if cons:
        inner.append(Field(**cons))
    ann: Any = Annotated[_params(base, inner)] if inner else base
    if not required and default is None and base is not Any:
        ann = Optional[ann]  # noqa: UP045 - ann is a runtime value
    if outer:
        ann = Annotated[_params(ann, outer)]
    replaced_by, deprecated = _deprecated(var)
    if replaced_by:
        ann = Annotated[_params(ann, [Meta(replaced_by=replaced_by)])]
    description = var.get("description", "")
    # details are docs only (SPEC §4.2): checked like a declaration's, never read at runtime.
    extra = {"details": var["details"]} if var.get("details") is not None else None
    if required:
        return ann, Field(description=description, json_schema_extra=extra, deprecated=deprecated)
    return ann, Field(
        default,
        description=description,
        validate_default=validate_default,
        json_schema_extra=extra,
        deprecated=deprecated,
    )


def _deprecated(entry: Mapping[str, Any]) -> tuple[str | None, Any]:
    """``(replacedBy, message)`` of an input's ``deprecated``; the declaration check applies its rules."""
    dep = entry.get("deprecated")
    if dep is None:
        return None, None
    if not isinstance(dep, Mapping):
        return None, True  # no message: rejected as blank
    return dep.get("replacedBy"), dep.get("message", "")


def field_name(input_name: str) -> str:
    """The settings field of a file input: its name with ``_`` for ``-`` (``serving-tls`` is ``serving_tls``)."""
    name = input_name.replace("-", "_")
    # A keyword, or a name the settings class already has (value, model_config...), gets a trailing _.
    return name + "_" if keyword.iskeyword(name) or hasattr(ContractSettings, name) else name


def _file_field(name: str, f: Mapping[str, Any], problems: list[str]) -> tuple[Any, Any] | None:
    """The annotation and ``Field`` a hand-written declaration would use for the file input ``f``."""
    ftype = f.get("type")
    if ftype not in _FILE_TYPES:
        problems.append(f"file {name}: unknown type {ftype!r}")
        return None
    common: dict[str, Any] = {
        "path": f.get("path", ""),
        "name": name,
        "path_env": f.get("pathEnv"),
        "reload": f.get("reload", "restart"),
        "max_size": f.get("maxSize"),
        "required": bool(f.get("required", False)),
        "group": f.get("group"),
    }
    meta: list[Any] = []
    base: Any
    if ftype == "config":
        fmt = f.get("format") or FORMAT_BY_SUFFIX.get(posixpath.splitext(common["path"])[1])
        base = Any
        schema = f.get("schema") or None
        if schema:
            try:
                schema_validator(schema)
            except ImportError:
                pass  # JsonValue logs that the schema is not checked
            except Exception as e:  # jsonschema.SchemaError
                problems.append(f"file {name}: invalid JSON Schema: {getattr(e, 'message', e)}")
                return None
        meta.append(ConfigFile(format=fmt, schema=schema or {}, **common))
    elif ftype == "tls":
        base = TlsKeyPair
        meta.append(
            TlsFile(
                dns_names=tuple(f.get("dnsNames", ())),
                key_algorithms=tuple(f.get("keyAlgorithms", ())),
                min_remaining=f.get("minRemaining"),
                require_ca=bool(f.get("requireCA", False)),
                **common,
            )
        )
    elif ftype == "caBundle":
        base = CaBundle
        meta.append(CaBundleFile(min_certificates=f.get("minCertificates", 1), **common))
    elif ftype == "keystore":
        base = Keystore
        meta.append(KeystoreFile(password_var=f.get("passwordVar"), format=f.get("format", "pkcs12"), **common))
    elif ftype == "text":
        base = str
        meta.append(
            TextFile(pattern=f.get("pattern"), min_length=f.get("minLength"), max_length=f.get("maxLength"), **common)
        )
    else:
        base = bytes
        meta.append(BinaryFile(**common))
    if f.get("secret"):
        meta.append(Secret())
    replaced_by, deprecated = _deprecated(f)
    if replaced_by:
        meta.append(Meta(replaced_by=replaced_by))
    ann: Any = Annotated[_params(base, meta)]
    extra = {"details": f["details"]} if f.get("details") is not None else None
    description = f.get("description", "")
    if common["required"]:
        return ann, Field(description=description, json_schema_extra=extra, deprecated=deprecated)
    if base is not Any:
        # Any already takes None, and Python 3.10 cannot put a marker holding a dict schema in a Union.
        ann = Optional[ann]  # noqa: UP045 - ann is a runtime value
    return ann, Field(None, description=description, json_schema_extra=extra, deprecated=deprecated)


def _params(base: Any, metadata: list[Any]) -> Any:
    """``Annotated[base, *metadata]``, spelled so Python 3.10 accepts it."""
    return (base, *metadata)


def _duration(value: Any) -> timedelta | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"duration {value!r} must be a string in Go form")
    return durations.ns_to_timedelta(durations.parse_go_duration(value))


def selected_profile(contract: Mapping[str, Any], env: Mapping[str, str]) -> str | None:
    """The profile in effect (SPEC §4.4): the selector's value when ``env`` sets it, else ``profiles.default``.

    For a ``string`` selector the empty string is a value, which names a
    profile with no file; for any other type, empty means unset.
    """
    profiles = contract.get("profiles")
    if not isinstance(profiles, Mapping):
        return None
    selector = profiles.get("selector")
    raw = env.get(selector) if isinstance(selector, str) else None
    sel_type = ((contract.get("vars") or {}).get(selector) or {}).get("type")
    if raw is not None and (raw != "" or sel_type == "string"):
        return raw
    default = profiles.get("default")
    return default if isinstance(default, str) else None


def contract_settings(
    contract: Mapping[str, Any] | str | os.PathLike[str], *, profile: str | None = None
) -> type[ContractSettings]:
    """Build the settings class for ``contract`` (a mapping, JSON text or the path of a ``contract.json``).

    ``profile`` selects a profile (SPEC §4.4): its ``profiles.defaults``
    become the variables' defaults. :func:`load_contract` picks it from the
    environment.

    Raises :class:`DeclarationError` when the contract is not one docuconf can
    load: an unknown type or encoding, a non-RE2 pattern, a default that breaks
    its own constraints, or a deprecated required input.
    """
    data = _read(contract)
    problems: list[str] = []
    if data.get("apiVersion") != API_VERSION or data.get("kind") != "ConfigContract":
        problems.append(f'not a contract: want apiVersion "{API_VERSION}" and kind "ConfigContract"')
    vars_ = data.get("vars") or {}
    if not isinstance(vars_, Mapping):
        problems.append("vars must be an object")
        vars_ = {}
    files = data.get("files") or {}
    if not isinstance(files, Mapping):
        problems.append("files must be an object")
        files = {}
    profile_defaults: Mapping[str, Any] = {}
    profiles = data.get("profiles")
    if profiles is not None:
        if not isinstance(profiles, Mapping) or not isinstance(profiles.get("defaults", {}), Mapping):
            problems.append("profiles must be an object with defaults by profile name")
        else:
            selector = profiles.get("selector")
            if selector not in vars_:
                problems.append(f"profiles.selector {selector!r} must name a declared variable")
            if profile is not None:
                profile_defaults = profiles.get("defaults", {}).get(profile) or {}
            for var_name in profile_defaults:
                if var_name not in vars_:
                    problems.append(f"profiles.defaults.{profile}: {var_name} is not a declared variable")
    fields: dict[str, Any] = {}
    inputs: dict[str, str] = {}
    for name in sorted(vars_):
        f = _field(name, vars_[name], problems, profile_defaults.get(name, _UNSET))
        if f is not None:
            fields[name] = f
            inputs[name] = name
    for name in sorted(files):
        f = _file_field(name, files[name], problems)
        if f is not None:
            fields[field_name(name)] = f
            inputs[name] = field_name(name)
    if problems:
        raise DeclarationError(problems)
    service = (data.get("metadata") or {}).get("name") or "contract"
    try:
        cls: type[ContractSettings] = create_model(
            "ContractSettings", __base__=ContractSettings, __module__=__name__, **fields
        )
    except Exception as e:  # pydantic rejects a constraint (a pattern its regex engine cannot compile, say)
        raise DeclarationError([f"cannot load the contract: {str(e).strip().splitlines()[-1]}"]) from e
    cls.docuconf_service = service  # type: ignore[attr-defined]
    cls.docuconf_inputs = inputs
    return cls


def _overlay_specs(data: Mapping[str, Any]) -> list[tuple[OverlaySpec, str]]:
    """The contract's overlays, with each one's ``keySeparator``."""
    out: list[tuple[OverlaySpec, str]] = []
    problems: list[str] = []
    overlays = data.get("overlays") or {}
    if not isinstance(overlays, Mapping):
        raise DeclarationError(["overlays must be an object"])
    for name, o in overlays.items():
        fmt = o.get("format")
        path = o.get("path")
        sep = o.get("keySeparator")
        if fmt not in ("json", "yaml", "toml") or not isinstance(path, str) or not isinstance(sep, str) or not sep:
            problems.append(f"overlay {name}: needs a format (json, yaml or toml), a path and a keySeparator")
            continue
        spec = OverlaySpec(name, fmt, path, o.get("reload", "restart"), o.get("description"), Overlay(name, path, fmt))
        out.append((spec, sep))
    if problems:
        raise DeclarationError(problems)
    return out


def _lookup_key(doc: Mapping[str, Any], parts: list[str]) -> tuple[bool, Any]:
    """The value at a key path, matching keys exactly, as ``#Render`` writes them."""
    cur: Any = doc
    for k in parts:
        if not isinstance(cur, Mapping) or k not in cur:
            return False, None
        cur = cur[k]
    return True, cur


def _overlay_layers(
    data: Mapping[str, Any], cls: type[ContractSettings], decl: Declaration, root: str | None
) -> tuple[dict[str, Layer], list[Violation]]:
    """Each variable's value from the contract's overlays (SPEC §4.7), and the overlays' own violations.

    Each overlay is read from under the file root, as declaration mode reads
    one; a missing file adds nothing. A value sits at its variable's
    ``configKey``, split on the overlay's ``keySeparator``, and becomes the
    wire string it stands for, checked later exactly like an env value. The
    first overlay that sets a variable wins.
    """
    layers: dict[str, Layer] = {}
    violations: list[Violation] = []
    profiles = data.get("profiles")
    selector = profiles.get("selector") if isinstance(profiles, Mapping) else None
    vars_ = data.get("vars") or {}
    for spec, sep in _overlay_specs(data):
        doc, bad = read_overlay(cls, spec, root)
        if bad is not None:
            violations.append(bad)
            continue
        for v in decl.vars:
            config_key = (vars_.get(v.name) or {}).get("configKey")
            if not config_key or v.name == selector or v.name in layers:
                continue  # the selector picks which files load, so no file sets it
            found, value = _lookup_key(doc, config_key.split(sep))
            if not found or value is None:
                continue  # null is unset
            source = f"overlay {spec.name}"
            if v.secret:
                # Never take, or print, a secret from an overlay: it is a ConfigMap.
                msg = f"is secret, but {source} sets it at {config_key}; supply secrets through the environment"
                violations.append(Violation(v.name, "var", "invalid_type", msg))
                layers[v.name] = Layer(source, bad=True)
                continue
            raw, items, problem = wire_value(v, value)
            if problem is not None:
                violations.append(Violation(v.name, "var", "invalid_type", f"{source}, at {config_key}: {problem}"))
                layers[v.name] = Layer(source, bad=True)
                continue
            layers[v.name] = Layer(source, raw=raw, items=items)
    return layers, violations


def load_contract(
    contract: Mapping[str, Any] | str | os.PathLike[str],
    env: Mapping[str, str] | None = None,
    *,
    termination_log: str | bool | None = None,
    now: datetime | None = None,
    watch: bool | None = None,
    watch_interval: float = 2.0,
) -> ContractSettings:
    """Validate ``env`` (default: the process environment) against ``contract`` and return the typed values.

    ``contract`` is the contract as JSON (``cue export contract.cue``): a
    mapping, JSON text or a path. The result is a pydantic-settings object with
    one attribute per input (see :class:`ContractSettings`); ``model_dump()``
    gives a dict. Every encoding in SPEC §5 is read: lists and key sets as
    ``csv``, ``json`` or ``indexed`` (``NAME__0``, ``NAME__1``...), durations as
    ``go``, ``iso8601``, ``seconds`` or ``timespan``.

    The whole contract is loaded, with the layers in SPEC §4.7's order: each
    variable's default, then the selected profile's (``profiles``), then the
    config-file overlays (``overlays``), then ``env``. File inputs and
    overlays are read from their paths under ``DOCUCONF_FILE_ROOT`` (from
    ``env``) and checked as :func:`docuconf.load` checks them; ``now`` is the
    time certificates are checked at.

    File inputs and overlays with ``reload: "watch"`` are polled every
    ``watch_interval`` seconds and reloaded in place, as :func:`docuconf.load`
    does (see :func:`docuconf.get_watcher`); ``watch`` defaults to on when
    ``env`` is not given. A reload reuses the environment read here, so a
    keystore password is not re-read.

    Raises :class:`ConfigValidationError` listing every violation, written to
    the termination log as :func:`docuconf.load` does.
    """
    data = _read(contract)
    values = dict(os.environ if env is None else env)
    cls = contract_settings(data, profile=selected_profile(data, values))
    decl = declaration(cls)
    environment = Env(cls, decl, values)
    results, file_kwargs, file_fields, violations = load_files(decl, environment, now)
    layers, more = _overlay_layers(data, cls, decl, environment.file_root)
    violations += more
    settings, more = build(cls, decl, environment, file_kwargs, file_fields, {}, layers=layers)
    violations += more
    if violations or settings is None:
        violations.sort(key=lambda v: v.input)
        err = ConfigValidationError(violations)
        if termination_log is not False:
            write_termination_log(str(err), termination_log if isinstance(termination_log, str) else None)
        raise err
    overlays = [spec for spec, _ in _overlay_specs(data)]
    if (watch if watch is not None else env is None) and (
        any(f.marker.reload == "watch" for f in decl.files) or any(o.reload == "watch" for o in overlays)
    ):
        from .watch import Watcher

        def rebuild(kwargs: dict[str, Any], fields: set[str]) -> tuple[ContractSettings | None, list[Violation]]:
            # The overlays as they are now, over the environment read at load.
            layers, bad = _overlay_layers(data, cls, decl, environment.file_root)
            new, more = build(cls, decl, environment, kwargs, fields, {}, layers=layers, warn=False)
            return new, bad + more

        Watcher.start(settings, decl, environment, results, interval=watch_interval, overlays=overlays, rebuild=rebuild)
    return settings
