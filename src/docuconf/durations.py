"""Go duration syntax, as used in contracts (SPEC §4.3, §11.2 item 3)."""

from __future__ import annotations

import re
from datetime import timedelta

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
    """Parse a Go duration (``time.ParseDuration``) into nanoseconds.

    Raises ``ValueError`` for invalid input.
    """
    s = text
    sign = 1
    if s[:1] in ("-", "+"):
        sign = -1 if s[0] == "-" else 1
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
    return sign * total


def format_go_duration(ns: int) -> str:
    """Format nanoseconds as a canonical contract duration: ``5400s`` -> ``1h30m``."""
    if ns < 0:
        raise ValueError("a contract duration cannot be negative")
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
    return timedelta(microseconds=ns // 1000)


def to_go(value: timedelta | str) -> str:
    """Canonical Go form of a ``timedelta`` or a Go duration string."""
    ns = timedelta_to_ns(value) if isinstance(value, timedelta) else parse_go_duration(value)
    return format_go_duration(ns)
