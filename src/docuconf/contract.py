"""Contract-first mode (SPEC §11.2 item 11): validate an environment against a contract given as data.

There is no in-language declaration. docuconf turns the contract into a
pydantic-settings class whose fields carry the same constraints and markers a
hand-written declaration would, then loads it through the same code as
:func:`docuconf.load`, so both modes share every check.
"""

from __future__ import annotations

import dataclasses
import json
import keyword
import os
import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Optional

from pydantic import Field, TypeAdapter, ValidationError, create_model
from pydantic_settings import BaseSettings, NoDecode, PydanticBaseSettingsSource, SettingsConfigDict

from . import durations
from .declaration import FORMAT_BY_SUFFIX, Declaration, OverlaySpec, declaration
from .errors import ConfigValidationError, DeclarationError, Violation, write_termination_log
from .files import FileResult, load_file
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
#: The variable that names the workload's role (SPEC §4.9).
ROLE_VAR = "DOCUCONF_ROLE"
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_ROLE_NAME = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
_FRAGMENT_NAME = re.compile(r"[a-z][a-z0-9-]*@[1-9][0-9]*")
#: The types a requiredIf condition variable can have: they compare as written (SPEC §4.2.1).
_CONDITION_TYPES = ("string", "int", "bool", "enum")


@dataclass(frozen=True)
class RequiredIf:
    """A condition that makes an input required (SPEC §4.2.1): while the variable ``var`` has ``value``.

    ``value`` is a ``str``, an ``int`` or a ``bool``, never a ``bool`` standing in for an ``int``.
    """

    var: str
    value: str | int | bool

    def holds_for(self, value: Any) -> bool:
        """Whether a variable's loaded value is the condition's value (``True`` is not ``1``)."""
        return type(value) is type(self.value) and value == self.value

    def __str__(self) -> str:
        """The condition as messages write it, the value as JSON: ``LOG_FORMAT is "json"``."""
        return f"{self.var} is {json.dumps(self.value, ensure_ascii=False)}"


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
    #: The well-known fragments the contract includes (SPEC §4.8), ``name@major``. Their variables are in ``vars``.
    docuconf_includes: ClassVar[tuple[str, ...]] = ()
    #: The contract's roles (SPEC §4.9); empty when it has none.
    docuconf_roles: ClassVar[tuple[str, ...]] = ()
    #: The role the class was built for: its inputs of other roles are optional. None with no (valid) role.
    docuconf_role: ClassVar[str | None] = None
    #: Each input with a ``requiredIf`` (SPEC §4.2.1), by input name.
    docuconf_required_if: ClassVar[dict[str, RequiredIf]] = {}
    #: Each input with ``roles``, by input name: the roles that use it.
    docuconf_input_roles: ClassVar[dict[str, tuple[str, ...]]] = {}

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


def _read_roles(node: Any, what: str, problems: list[str]) -> tuple[str, ...] | None:
    """A list of roles (SPEC §4.9): DNS labels, at least one, none listed twice. None when absent."""
    if node is None:
        return None
    if not isinstance(node, list) or not node or not all(isinstance(r, str) for r in node):
        problems.append(f"{what}roles must be a list of at least one role name")
        return None
    for i, r in enumerate(node):
        if not _ROLE_NAME.fullmatch(r):
            problems.append(f"{what}role {r!r} must be a DNS label")
        elif r in node[:i]:
            problems.append(f"{what}role {r} is listed twice")
    return tuple(node)


def _read_includes(node: Any, problems: list[str]) -> tuple[str, ...]:
    """The well-known fragments the contract includes (SPEC §4.8), ``name@major``: their variables are inline."""
    if node is None:
        return ()
    if not isinstance(node, list) or not all(isinstance(x, str) for x in node):
        problems.append("includes must be a list of fragment names, such as otel@1")
        return ()
    out: list[str] = []
    for name in node:
        if not _FRAGMENT_NAME.fullmatch(name):
            problems.append(f"includes: {name!r} is not a fragment name, name@major")
        elif name in out:
            problems.append(f"includes: {name} is listed twice")
        else:
            out.append(name)
    return tuple(out)


def _read_required_if(entry: Mapping[str, Any], what: str, problems: list[str]) -> RequiredIf | None:
    """``requiredIf: {VAR: value}`` (SPEC §4.2.1): one variable and a string, integer or boolean value."""
    node = entry.get("requiredIf")
    if node is None:
        return None
    if not isinstance(node, Mapping) or len(node) != 1:
        problems.append(f"{what}requiredIf must name one variable and its value: {{VAR: value}}")
        return None
    ((var, value),) = node.items()
    # bool is an int in Python; both are accepted here, and kept apart when compared with the variable's type.
    if not isinstance(var, str) or not _ENV_NAME.fullmatch(var) or not isinstance(value, (str, int)):
        problems.append(f"{what}requiredIf must name one variable and a string, integer or boolean value")
        return None
    if entry.get("required") or entry.get("default") is not None:
        problems.append(f"{what}an input with requiredIf cannot be required or have a default")
    if entry.get("deprecated") is not None:
        problems.append(
            f"{what}a conditionally required input cannot be deprecated: the platform could not stop setting it"
        )
    return RequiredIf(var, value)


def _check_condition(
    what: str,
    name: str,
    cond: RequiredIf,
    vars_: Mapping[str, Any],
    fields: Mapping[str, Any],
    problems: list[str],
) -> None:
    """A requiredIf names another declared, plain string, int, bool or enum variable, with a value it can hold."""
    on = vars_.get(cond.var)
    if cond.var == name or not isinstance(on, Mapping):
        problems.append(f"{what}requiredIf must name another declared variable, not {cond.var!r}")
        return
    vtype = on.get("type")
    if on.get("secret") or vtype not in _CONDITION_TYPES:
        problems.append(f"{what}requiredIf names {cond.var}, which is not a plain string, int, bool or enum variable")
        return
    want: type = {"int": int, "bool": bool}.get(vtype, str)
    detail = ""
    ok = type(cond.value) is want
    if ok and cond.var in fields:
        try:
            TypeAdapter(fields[cond.var][0]).validate_python(cond.value, strict=True)
        except ValidationError as e:
            ok, detail = False, ": " + e.errors()[0]["msg"]
    if not ok:
        problems.append(f"{what}requiredIf: {cond} is not a value {cond.var} can have{detail}")


def _check_roles(
    roles: tuple[str, ...] | None,
    input_roles: Mapping[str, tuple[str, ...]],
    labels: Mapping[str, str],
    vars_: Mapping[str, Any],
    problems: list[str],
) -> None:
    """The inputs' roles against the contract's (SPEC §4.9), and DOCUCONF_ROLE: a required enum of exactly the roles."""
    for name, used in input_roles.items():
        what = labels[name]
        if roles is None:
            problems.append(f"{what}has roles, but the contract declares none")
            continue
        for r in used:
            if r not in roles:
                problems.append(f"{what}role {r} is not one of the contract's roles, {', '.join(roles)}")
    if roles is None:
        return
    role_var = vars_.get(ROLE_VAR)
    if not (
        isinstance(role_var, Mapping)
        and role_var.get("type") == "enum"
        and role_var.get("required") is True
        and role_var.get("roles") is None
        and list(role_var.get("values") or []) == list(roles)
    ):
        problems.append(
            f"{ROLE_VAR}: a contract with roles declares it as a required enum whose values are the roles, "
            f"{', '.join(roles)}, used by every role"
        )


def selected_role(contract: Mapping[str, Any], env: Mapping[str, str]) -> str | None:
    """The workload's role (SPEC §4.9): ``DOCUCONF_ROLE`` from ``env`` when it is one of the contract's roles.

    None when the contract has no roles, or the variable is unset or names
    another role; the load then reports that as the one problem about roles.
    """
    roles = contract.get("roles")
    role = env.get(ROLE_VAR)
    return role if isinstance(roles, list) and role in roles else None


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
    contract: Mapping[str, Any] | str | os.PathLike[str], *, profile: str | None = None, role: str | None = None
) -> type[ContractSettings]:
    """Build the settings class for ``contract`` (a mapping, JSON text or the path of a ``contract.json``).

    ``profile`` selects a profile (SPEC §4.4): its ``profiles.defaults``
    become the variables' defaults. ``role`` is the workload's role (SPEC
    §4.9): an input whose ``roles`` do not include it is optional, and with no
    role every input that has ``roles`` is. :func:`load_contract` picks both
    from the environment. The class records each input's ``requiredIf``
    (SPEC §4.2.1), which :func:`load_contract` checks once the variables are
    loaded, and the contract's ``includes`` (SPEC §4.8) and ``roles``.

    Raises :class:`DeclarationError` when the contract is not one docuconf can
    load: an unknown type or encoding, a non-RE2 pattern, a default that breaks
    its own constraints, a deprecated required input, a ``requiredIf`` that
    does not name another plain variable and a value it can hold, or ``roles``
    that do not match the contract's and ``DOCUCONF_ROLE``.
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
    includes = _read_includes(data.get("includes"), problems)
    roles = _read_roles(data.get("roles"), "", problems)
    if role is not None and (roles is None or role not in roles):
        role = None
    entries: dict[str, tuple[str, Mapping[str, Any]]] = {}  # input name: (message prefix, contract entry)
    for name in sorted(vars_):
        if isinstance(vars_[name], Mapping):
            entries[name] = (f"{name}: ", vars_[name])
    for name in sorted(files):
        if isinstance(files[name], Mapping):
            entries.setdefault(name, (f"file {name}: ", files[name]))
    required_if: dict[str, RequiredIf] = {}
    input_roles: dict[str, tuple[str, ...]] = {}
    for name, (what, entry) in entries.items():
        cond = _read_required_if(entry, what, problems)
        if cond is not None:
            required_if[name] = cond
        used = _read_roles(entry.get("roles"), what, problems)
        if used is not None:
            input_roles[name] = used
    _check_roles(roles, input_roles, {n: w for n, (w, _) in entries.items()}, vars_, problems)

    def for_role(name: str, entry: Mapping[str, Any]) -> Mapping[str, Any]:
        # An input only other roles use is optional in this one (SPEC §4.9).
        if name in input_roles and role not in input_roles[name] and entry.get("required"):
            return {**entry, "required": False}
        return entry

    fields: dict[str, Any] = {}
    inputs: dict[str, str] = {}
    for name in sorted(vars_):
        f = _field(name, for_role(name, vars_[name]), problems, profile_defaults.get(name, _UNSET))
        if f is not None:
            fields[name] = f
            inputs[name] = name
    for name in sorted(files):
        f = _file_field(name, for_role(name, files[name]), problems)
        if f is not None:
            fields[field_name(name)] = f
            inputs[name] = field_name(name)
    for name, cond in required_if.items():
        _check_condition(entries[name][0], name, cond, vars_, fields, problems)
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
    cls.docuconf_includes = includes
    cls.docuconf_roles = roles or ()
    cls.docuconf_role = role
    cls.docuconf_required_if = required_if
    cls.docuconf_input_roles = input_roles
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


def _loaded(
    name: str, var: Mapping[str, Any], env: Env, layers: Mapping[str, Layer], defaults: Mapping[str, Any]
) -> Any:
    """The loaded value of a plain (string, int, bool or enum) variable with no violation, or ``_UNSET``.

    The layers in SPEC §4.7's order, top first: the environment (empty is
    unset but for a string), an overlay, the selected profile, the default.
    """
    raw = env(name)
    if raw is None or (raw == "" and var.get("type") != "string"):
        layer = layers.get(name)
        raw = layer.raw if layer is not None and not layer.bad else None
    if raw is not None:
        if var.get("type") == "bool":
            return raw.lower() == "true"
        return int(raw) if var.get("type") == "int" else raw
    if name in defaults:
        return defaults[name]
    default = var.get("default")
    return _UNSET if default is None else default


def _is_set(
    name: str, var: Mapping[str, Any], env: Env, layers: Mapping[str, Layer], defaults: Mapping[str, Any]
) -> bool:
    """Whether the variable ``name`` (an input with no default) has a value from any layer."""
    if name in layers or defaults.get(name) is not None:
        return True
    if var.get("type") in ("list", "keySet") and var.get("encoding") == "indexed":
        items, gap = env.indexed((name,))
        return items is not None or gap is not None
    raw = env(name)
    return raw is not None and (raw != "" or var.get("type") == "string")


def _conditions(
    data: Mapping[str, Any],
    cls: type[ContractSettings],
    env: Env,
    layers: Mapping[str, Layer],
    defaults: Mapping[str, Any],
    results: list[FileResult],
    violations: Sequence[Violation],
    now: datetime,
) -> list[Violation]:
    """Conditional requirements (SPEC §4.2.1), on the condition variables' loaded values.

    An input whose condition holds, and that its role uses (SPEC §4.9), is
    required: a missing variable is ``missing_required``, a missing file
    ``file_missing``, each saying which condition requires it. A condition
    variable with a violation of its own has no value, so it makes nothing
    required. A file input made required replaces its result in ``results``,
    so a reload checks it as required too.
    """
    if not cls.docuconf_required_if:
        return []
    vars_ = data.get("vars") or {}
    reported = {v.input for v in violations}
    role = cls.docuconf_role

    def when(name: str) -> RequiredIf | None:
        cond = cls.docuconf_required_if.get(name)
        used = cls.docuconf_input_roles.get(name)
        if cond is None or (used is not None and role not in used) or cond.var in reported:
            return None
        value = _loaded(cond.var, vars_[cond.var], env, layers, defaults)
        return cond if cond.holds_for(value) else None

    out: list[Violation] = []
    for name in sorted(vars_):
        if (
            name in cls.docuconf_required_if
            and name not in reported
            and not _is_set(name, vars_[name], env, layers, defaults)
        ):
            cond = when(name)
            if cond is not None:
                out.append(Violation(name, "var", "missing_required", f"is required when {cond}"))
    for i, r in enumerate(results):
        if r.present or r.violations or r.spec.required:
            continue
        cond = when(r.spec.name)
        if cond is None:
            continue
        again = load_file(dataclasses.replace(r.spec, required=True), env, now=now, root=env.file_root)
        again.violations = [
            dataclasses.replace(v, message=f"{v.message}, and is required when {cond}")
            if v.code == "file_missing"
            else v
            for v in again.violations
        ]
        results[i] = again
        out.extend(again.violations)
    return out


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
    profile = selected_profile(data, values)
    cls = contract_settings(data, profile=profile, role=selected_role(data, values))
    decl = declaration(cls)
    environment = Env(cls, decl, values)
    now = now or datetime.now(timezone.utc)
    results, file_kwargs, file_fields, violations = load_files(decl, environment, now)
    layers, more = _overlay_layers(data, cls, decl, environment.file_root)
    violations += more
    settings, more = build(cls, decl, environment, file_kwargs, file_fields, {}, layers=layers)
    violations += more
    profiles = data.get("profiles")
    defaults = (profiles.get("defaults") or {}).get(profile) or {} if isinstance(profiles, Mapping) else {}
    violations += _conditions(data, cls, environment, layers, defaults, results, violations, now)
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
