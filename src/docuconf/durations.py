"""Go duration syntax, as used in contracts (SPEC §4.3, §11.2 item 3)."""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal
from typing import Literal

#: Wire encodings of a duration variable (SPEC §5).
Encoding = Literal["go", "iso8601", "seconds", "timespan"]

_UNIT_NS = {
    "ns": 1,
    "us": 1_000,
    "µs": 1_000,
    "μs": 1_000,
    "ms": 1_000_000,
    "s": 10**9,
    "m": 60 * 10**9,
    "h": 3600 * 10**9,
}
_PART = re.compile(r"([0-9]*(?:\.[0-9]*)?)(ns|us|µs|μs|ms|s|m|h)")

#: The contract's ``#Duration`` form: integer components, no sign or fractions.
CONTRACT_DURATION = re.compile(r"^([0-9]+(ns|us|ms|s|m|h))+$")


def parse_go_duration(text: str) -> int:
    """Parse a Go duration (``time.ParseDuration``, SPEC §5) into nanoseconds.

    An optional sign, then ``0`` or numbers with a unit (``1m30s``, ``1.5h``,
    ``.5s``, ``1.s``). Units are lower case; nothing is trimmed. The result is
    truncated to whole nanoseconds. Raises ``ValueError`` for anything else,
    and for a value beyond +/-(2^63-1) nanoseconds.
    """
    s = text
    neg = False
    if s[:1] in ("-", "+"):
        neg = s[0] == "-"
        s = s[1:]
    if s == "0":
        return 0
    if s == "":
        raise ValueError(f"invalid duration {text!r}")
    total = 0
    pos = 0
    while pos < len(s):
        m = _PART.match(s, pos)
        if not m or not re.search(r"[0-9]", m.group(1)):
            raise ValueError(f"invalid duration {text!r}")
        num, unit = m.group(1), m.group(2)
        whole, _, frac = num.partition(".")
        ns = int(whole or "0") * _UNIT_NS[unit]
        if frac:
            ns += int(frac) * _UNIT_NS[unit] // 10 ** len(frac)
        total += ns
        pos = m.end()
    if total > (2**63 if neg else 2**63 - 1):
        raise ValueError(f"duration {text!r} is out of range")
    return -total if neg else total


def format_go_duration(ns: int, *, signed: bool = False) -> str:
    """Format nanoseconds as a canonical contract duration: ``5400s`` -> ``1h30m``.

    A contract duration is never negative; with ``signed``, a negative value
    is written with a leading ``-`` (``-1m30s``), as a typed value may be.
    """
    if ns < 0:
        if not signed:
            raise ValueError("a contract duration cannot be negative")
        return "-" + format_go_duration(-ns)
    if ns == 0:
        return "0s"
    out = []
    for unit, size in (("h", 3600 * 10**9), ("m", 60 * 10**9), ("s", 10**9), ("ms", 10**6), ("us", 10**3), ("ns", 1)):
        q, ns = divmod(ns, size)
        if q:
            out.append(f"{q}{unit}")
    return "".join(out)


def timedelta_to_ns(td: timedelta) -> int:
    return (td.days * 86400 + td.seconds) * 10**9 + td.microseconds * 1000


def ns_to_timedelta(ns: int) -> timedelta:
    """Nanoseconds as a ``timedelta``, truncated toward zero to whole microseconds."""
    return timedelta(microseconds=ns // 1000 if ns >= 0 else -(-ns // 1000))


def to_go(value: timedelta | str, *, signed: bool = False) -> str:
    """Canonical Go form of a ``timedelta`` or a Go duration string (see :func:`format_go_duration`)."""
    ns = timedelta_to_ns(value) if isinstance(value, timedelta) else parse_go_duration(value)
    return format_go_duration(ns, signed=signed)


_SECONDS = re.compile(r"^[0-9]+(\.[0-9]+)?$")
# .NET TimeSpan's constant format: [d.]hh:mm:ss[.fffffff]
_TIMESPAN = re.compile(r"^(?:([0-9]+)\.)?([0-9]{1,2}):([0-9]{2}):([0-9]{2})(\.[0-9]{1,7})?$")
_ISO_NUM = r"([0-9]+(?:[.,][0-9]+)?)"
# P[nD][T[nH][nM][nS]]: upper case, unsigned, no years, months or weeks (SPEC §5).
_ISO8601 = re.compile(rf"^P(?:{_ISO_NUM}D)?(?:T(?:{_ISO_NUM}H)?(?:{_ISO_NUM}M)?(?:{_ISO_NUM}S)?)?$")


def parse_seconds(text: str) -> int:
    """Parse a decimal number of seconds (``90``, ``1.5``) into nanoseconds."""
    if not _SECONDS.match(text):
        raise ValueError(f"invalid duration in seconds {text!r}")
    return int(Decimal(text) * 10**9)


def parse_timespan(text: str) -> int:
    """Parse a .NET TimeSpan (``[d.]hh:mm:ss[.fff]``) into nanoseconds."""
    m = _TIMESPAN.match(text)
    if not m:
        raise ValueError(f"invalid TimeSpan {text!r}")
    days, hours, minutes, seconds, frac = m.groups()
    if int(hours) > 23 or int(minutes) > 59 or int(seconds) > 59:
        raise ValueError(f"invalid TimeSpan {text!r}")
    total = ((int(days or 0) * 24 + int(hours)) * 60 + int(minutes)) * 60 + int(seconds)
    return total * 10**9 + (int(Decimal(frac) * 10**9) if frac else 0)


def parse_iso8601(text: str) -> int:
    """Parse an ISO 8601 duration (``PT1M30S``, ``P1DT2H``, ``PT1,5S``) into nanoseconds.

    Only days, hours, minutes and seconds, which have a fixed length; at least
    one component, and at least one after a ``T``. Upper case and unsigned.
    """
    m = _ISO8601.match(text)
    if not m or text in ("P", "PT") or text.endswith("T"):
        raise ValueError(f"invalid ISO 8601 duration {text!r}")
    total = Decimal(0)
    for num, size in zip(m.groups(), (86400, 3600, 60, 1), strict=True):
        if num is not None:
            total += Decimal(num.replace(",", ".")) * size
    return int(total * 10**9)


def parse_ns(text: str, encoding: Encoding) -> int:
    """Parse a duration in a wire encoding (SPEC §5) into nanoseconds; raises ``ValueError``."""
    if encoding == "go":
        return parse_go_duration(text)
    if encoding == "seconds":
        return parse_seconds(text)
    if encoding == "timespan":
        return parse_timespan(text)
    if encoding == "iso8601":
        return parse_iso8601(text)
    raise ValueError(f"unknown duration encoding {encoding!r}")


def parse(text: str, encoding: Encoding) -> timedelta:
    """Parse a duration in a wire encoding into a ``timedelta`` (truncated to microseconds)."""
    try:
        return ns_to_timedelta(parse_ns(text, encoding))
    except OverflowError:
        raise ValueError(f"duration {text!r} is out of range") from None
