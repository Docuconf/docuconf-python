"""The ``keySet`` type (SPEC §4.3, §6.1): secret keys that are all valid at once, so one can be rotated."""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler, SecretStr
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import PydanticCustomError, core_schema

REDACTED = "**********"

KeySetEncoding = Literal["csv", "json", "indexed"]


class KeySet:
    """A set of secret keys that are all valid at once (contract type ``keySet``).

    It is for the side that verifies: webhook signatures, inbound API keys,
    JWT HMAC verification, cookie-signing fallbacks. During a rotation the
    platform supplies the old and the new key (``old,new``), so no request
    signed with either is turned away::

        class Settings(DocuconfSettings):
            webhook_keys: Annotated[KeySet, Keys(key_min_length=32, key_max_length=256)] = Field(
                description="Keys that verify webhook signatures"
            )

        ok = settings.webhook_keys.verify(
            lambda key: hmac.compare_digest(hmac.new(key, body, hashlib.sha256).hexdigest(), signature)
        )

    A key set is always secret: ``repr()``, ``str()`` and ``model_dump(mode="json")``
    show ``'**********'``, and boot errors never contain a key. Use
    :class:`Keys` for its bounds and wire encoding; without it, a key set
    holds 1 or 2 comma-separated keys.
    """

    __slots__ = ("_keys",)

    def __init__(self, keys: Sequence[str | SecretStr]) -> None:
        self._keys: tuple[str, ...] = tuple(k.get_secret_value() if isinstance(k, SecretStr) else k for k in keys)
        if not all(isinstance(k, str) for k in self._keys):
            raise TypeError("KeySet keys must be strings")

    @property
    def keys(self) -> tuple[SecretStr, ...]:
        """The keys, in the order the platform gave them."""
        return tuple(SecretStr(k) for k in self._keys)

    def contains(self, candidate: str | bytes) -> bool:
        """Whether ``candidate`` is one of the keys, such as an API key a caller presents.

        Compares with every key in constant time, so the time taken does not
        say which key matched, or how much of one.
        """
        want = candidate.encode() if isinstance(candidate, str) else bytes(candidate)
        found = False
        for key in self._keys:
            found = hmac.compare_digest(key.encode(), want) | found
        return found

    def verify(self, check: Callable[[bytes], bool]) -> bool:
        """Call ``check(key)`` with each key (as UTF-8 bytes), and return whether any call returned true.

        For checks that need the key itself, such as an HMAC::

            ok = keys.verify(lambda key: hmac.compare_digest(hmac.new(key, body, "sha256").digest(), signature))

        Every key is tried, even after one matches, so the time taken does not
        say which key matched; ``check`` should compare in constant time
        itself, as ``hmac.compare_digest`` does.
        """
        ok = False
        for key in self._keys:
            ok = bool(check(key.encode())) | ok
        return ok

    def __len__(self) -> int:
        return len(self._keys)

    def __iter__(self) -> Iterator[SecretStr]:
        return iter(self.keys)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, KeySet):
            return NotImplemented
        return len(self._keys) == len(other._keys) and all(
            hmac.compare_digest(a.encode(), b.encode()) for a, b in zip(self._keys, other._keys, strict=True)
        )

    def __hash__(self) -> int:
        return hash(len(self._keys))

    def __repr__(self) -> str:
        return f"KeySet('{REDACTED}')"

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, spec: str) -> str:
        return format(REDACTED, spec)

    def __reduce__(self) -> Any:
        raise TypeError("a KeySet holds secrets and cannot be pickled")

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        return Keys()._schema()

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return {"type": "array", "items": {"type": "string"}, "writeOnly": True}


@dataclass(frozen=True)
class Keys:
    """The bounds and wire encoding of a :class:`KeySet` field (SPEC §4.3)::

        api_keys: Annotated[KeySet, Keys(max_keys=3, encoding="json")] | None = None

    The number of keys outside ``min_keys``..``max_keys`` is ``too_few_items``
    or ``too_many_items``; a key outside ``key_min_length``..``key_max_length``
    characters, and an empty key whatever the bounds (a stray separator), is
    ``out_of_range``. Keys are never trimmed.
    """

    #: The fewest keys (at least 1).
    min_keys: int = 1
    #: The most keys (at least ``min_keys``).
    max_keys: int = 2
    #: The shortest key, in characters (Unicode code points).
    key_min_length: int | None = None
    #: The longest key, in characters (at least 1).
    key_max_length: int | None = None
    #: ``csv`` (``old,new``), ``json`` (``["old","new"]``) or ``indexed`` (``NAME__0``, ``NAME__1``).
    encoding: KeySetEncoding = "csv"
    #: The separator of the ``csv`` encoding.
    separator: str = ","

    def problems(self) -> list[str]:
        """What is wrong with these bounds, for the declaration check."""
        out: list[str] = []

        def bad_int(x: Any) -> bool:
            return isinstance(x, bool) or not isinstance(x, int)

        if bad_int(self.min_keys) or self.min_keys < 1:
            out.append("min_keys must be at least 1")
        elif bad_int(self.max_keys) or self.max_keys < self.min_keys:
            out.append(f"max_keys must be at least min_keys ({self.min_keys})")
        for name in ("key_min_length", "key_max_length"):
            v = getattr(self, name)
            if v is not None and (bad_int(v) or v < 0):
                out.append(f"{name} must be a non-negative integer")
        if self.key_max_length is not None and not bad_int(self.key_max_length) and self.key_max_length < 1:
            out.append("key_max_length must be at least 1")
        lo, hi = self.key_min_length, self.key_max_length
        if lo is not None and hi is not None and not bad_int(lo) and not bad_int(hi) and lo > hi:
            out.append(f"key_min_length {lo} is above key_max_length {hi}")
        if self.encoding not in ("csv", "json", "indexed"):
            out.append('encoding must be "csv", "json" or "indexed"')
        if not isinstance(self.separator, str) or self.separator == "":
            out.append("separator must be a non-empty string")
        return out

    def contract(self) -> dict[str, Any]:
        """The contract fields after ``type``, ``description`` and ``secret``."""
        out: dict[str, Any] = {"encoding": self.encoding}
        if self.encoding == "csv":
            out["separator"] = self.separator
        out["minKeys"] = self.min_keys
        out["maxKeys"] = self.max_keys
        if self.key_min_length is not None:
            out["keyMinLength"] = self.key_min_length
        if self.key_max_length is not None:
            out["keyMaxLength"] = self.key_max_length
        return out

    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        return self._schema()

    def split(self, value: Any) -> list[str]:
        """The keys of a wire value (a string in this encoding) or a sequence; raises ``PydanticCustomError``."""
        if isinstance(value, KeySet):
            return list(value._keys)
        if isinstance(value, str):
            if self.encoding == "json":
                try:
                    data = json.loads(value)
                except ValueError:
                    raise PydanticCustomError("key_set_type", "should be a JSON array of strings") from None
            elif self.encoding == "indexed":
                # One key per NAME__<n> variable; a single string is one key.
                data = [value]
            else:
                data = value.split(self.separator)
        else:
            data = value
        if not isinstance(data, (list, tuple)) or not all(isinstance(k, (str, SecretStr)) for k in data):
            raise PydanticCustomError("key_set_type", "should be a list of string keys")
        return [k.get_secret_value() if isinstance(k, SecretStr) else k for k in data]

    def check(self, keys: Sequence[str]) -> None:
        """The bounds; messages give positions and lengths, never a key."""
        n = len(keys)
        if n < self.min_keys:
            raise PydanticCustomError(
                "key_set_too_few", "should have at least {min} key(s), has {n}", {"min": self.min_keys, "n": n}
            )
        if n > self.max_keys:
            raise PydanticCustomError(
                "key_set_too_many", "should have at most {max} key(s), has {n}", {"max": self.max_keys, "n": n}
            )
        for i, key in enumerate(keys):
            length = len(key)
            if length == 0:
                raise PydanticCustomError("key_set_key_length", "key {i} is empty", {"i": i + 1})
            if self.key_min_length is not None and length < self.key_min_length:
                raise PydanticCustomError(
                    "key_set_key_length",
                    "key {i} should have at least {min} characters, has {n}",
                    {"i": i + 1, "min": self.key_min_length, "n": length},
                )
            if self.key_max_length is not None and length > self.key_max_length:
                raise PydanticCustomError(
                    "key_set_key_length",
                    "key {i} should have at most {max} characters, has {n}",
                    {"i": i + 1, "max": self.key_max_length, "n": length},
                )

    def _schema(self) -> core_schema.CoreSchema:
        def validate(value: Any) -> KeySet:
            keys = self.split(value)
            self.check(keys)
            return KeySet(keys)

        return core_schema.no_info_plain_validator_function(
            validate,
            serialization=core_schema.plain_serializer_function_ser_schema(_serialize, info_arg=True),
        )


def _serialize(value: KeySet, info: core_schema.SerializationInfo) -> Any:
    # A key set is secret: like SecretStr, it dumps as itself in Python mode and as asterisks in JSON.
    return REDACTED if info.mode == "json" else value
