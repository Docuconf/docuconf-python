"""Contract-first mode (SPEC §11.2 item 11): validate an environment against a contract given as data.

There is no in-language declaration. docuconf turns the contract into a
pydantic-settings class whose fields carry the same constraints and markers a
hand-written declaration would, then loads it through the same code as
:func:`docuconf.load`, so both modes share every check.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Optional

from pydantic import Field, create_model
from pydantic_settings import BaseSettings, NoDecode, PydanticBaseSettingsSource, SettingsConfigDict

from . import durations
from .declaration import declaration
from .errors import ConfigValidationError, DeclarationError, write_termination_log
from .loader import ActiveEnvSource, Env, _RedactSecrets, build
from .markers import Csv, Duration, IndexedList, JsonMaxLength, JsonValue, Secret, Url, schema_validator
from .re2 import non_re2_feature

API_VERSION = "docuconf.dev/v1alpha1"
_VAR_TYPES = ("string", "int", "float", "bool", "duration", "url", "enum", "list", "json")


class ContractSettings(_RedactSecrets, BaseSettings):
    """Base of the settings classes :func:`load_contract` builds: one field per variable, named as the variable.

    ``repr()`` shows secret variables as ``'**********'``.
    """

    model_config = SettingsConfigDict(case_sensitive=True, extra="ignore")
    __docuconf_managed__: ClassVar[bool] = True

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


def _field(name: str, var: Mapping[str, Any], problems: list[str]) -> tuple[Any, Any] | None:
    """The annotation and ``Field`` a hand-written declaration would use for ``var``."""
    vtype = var.get("type")
    if vtype not in _VAR_TYPES:
        problems.append(f"{name}: unknown type {vtype!r}")
        return None
    required = bool(var.get("required", False))
    secret = bool(var.get("secret", False))
    default = var.get("default", None)
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
    if not required and default is None:
        ann = Optional[ann]  # noqa: UP045 - ann is a runtime value
    if outer:
        ann = Annotated[_params(ann, outer)]
    description = var.get("description", "")
    if required:
        return ann, Field(description=description)
    return ann, Field(default, description=description, validate_default=validate_default)


def _params(base: Any, metadata: list[Any]) -> Any:
    """``Annotated[base, *metadata]``, spelled so Python 3.10 accepts it."""
    return (base, *metadata)


def _duration(value: Any) -> timedelta | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"duration {value!r} must be a string in Go form")
    return durations.ns_to_timedelta(durations.parse_go_duration(value))


def contract_settings(contract: Mapping[str, Any] | str | os.PathLike[str]) -> type[ContractSettings]:
    """Build the settings class for ``contract`` (a mapping, JSON text or the path of a ``contract.json``).

    Raises :class:`DeclarationError` when the contract is not one docuconf can
    load: an unknown type or encoding, a non-RE2 pattern, a default that breaks
    its own constraints, or file inputs and overlays, which this mode does not
    load yet.
    """
    data = _read(contract)
    problems: list[str] = []
    if data.get("apiVersion") != API_VERSION or data.get("kind") != "ConfigContract":
        problems.append(f'not a contract: want apiVersion "{API_VERSION}" and kind "ConfigContract"')
    for key in ("files", "overlays"):
        if data.get(key):
            problems.append(f"{key}: the contract-first mode loads variables only")
    vars_ = data.get("vars") or {}
    if not isinstance(vars_, Mapping):
        problems.append("vars must be an object")
        vars_ = {}
    fields: dict[str, Any] = {}
    for name in sorted(vars_):
        f = _field(name, vars_[name], problems)
        if f is not None:
            fields[name] = f
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
    return cls


def load_contract(
    contract: Mapping[str, Any] | str | os.PathLike[str],
    env: Mapping[str, str] | None = None,
    *,
    termination_log: str | bool | None = None,
) -> ContractSettings:
    """Validate ``env`` (default: the process environment) against ``contract`` and return the typed values.

    ``contract`` is the contract as JSON (``cue export contract.cue``): a
    mapping, JSON text or a path. The result is a pydantic-settings object with
    one attribute per variable, named as the variable (``values.PORT``);
    ``model_dump()`` gives a dict. Every encoding in SPEC §5 is read: lists as
    ``csv``, ``json`` or ``indexed`` (``NAME__0``, ``NAME__1``...), durations as
    ``go``, ``iso8601``, ``seconds`` or ``timespan``.

    Raises :class:`ConfigValidationError` listing every violation, written to
    the termination log as :func:`docuconf.load` does.
    """
    cls = contract_settings(contract)
    decl = declaration(cls)
    environment = Env(cls, decl, dict(os.environ if env is None else env))
    settings, violations = build(cls, decl, environment, {}, set(), {})
    if violations or settings is None:
        violations.sort(key=lambda v: v.input)
        err = ConfigValidationError(violations)
        if termination_log is not False:
            write_termination_log(str(err), termination_log if isinstance(termination_log, str) else None)
        raise err
    return settings
