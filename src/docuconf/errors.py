"""Violations, error codes and the Kubernetes termination log."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

#: Stable error codes from SPEC §11.2 item 5.
ERROR_CODES: tuple[str, ...] = (
    "missing_required",
    "invalid_type",
    "out_of_range",
    "pattern_mismatch",
    "not_in_enum",
    "invalid_scheme",
    "too_few_items",
    "too_many_items",
    "file_missing",
    # The file exists but cannot be read, e.g. a root-owned 0400 secret
    # volume in a container that runs as another user.
    "file_unreadable",
    "file_too_large",
    "file_malformed",
    "schema_mismatch",
    "certificate_invalid",
    "certificate_expiring",
    "certificate_name_mismatch",
    "key_mismatch",
    "keystore_unreadable",
)

ErrorCode = Literal[
    "missing_required",
    "invalid_type",
    "out_of_range",
    "pattern_mismatch",
    "not_in_enum",
    "invalid_scheme",
    "too_few_items",
    "too_many_items",
    "file_missing",
    "file_unreadable",
    "file_too_large",
    "file_malformed",
    "schema_mismatch",
    "certificate_invalid",
    "certificate_expiring",
    "certificate_name_mismatch",
    "key_mismatch",
    "keystore_unreadable",
]


@dataclass(frozen=True)
class Violation:
    """One problem found while validating the environment or a file input at boot."""

    #: Variable name (``PORT``) or file input name (``serving-tls``).
    input: str
    kind: Literal["var", "file", "model"]
    code: ErrorCode
    #: Human-readable detail. Never contains a secret value.
    message: str

    def __str__(self) -> str:
        return f"{self.input} [{self.code}]: {self.message}"


def format_violations(violations: Sequence[Violation]) -> str:
    n = len(violations)
    lines = "\n".join(f"  - {v}" for v in violations)
    return f"docuconf: {n} configuration problem{'' if n == 1 else 's'}:\n{lines}"


class DocuconfError(Exception):
    """Base class for docuconf errors."""


class DocuconfWarning(UserWarning):
    """Warnings docuconf issues at boot: declaration warnings, and set variables that look like typos.

    Filter them like any warning, e.g. ``warnings.simplefilter("error", docuconf.DocuconfWarning)`` in tests.
    """


class ConfigValidationError(DocuconfError):
    """Raised by :func:`docuconf.load` when the environment or a file input is invalid.

    ``violations`` holds every problem found, not just the first.
    """

    def __init__(self, violations: Sequence[Violation]) -> None:
        self.violations: tuple[Violation, ...] = tuple(violations)
        super().__init__(format_violations(self.violations))

    @property
    def codes(self) -> list[str]:
        return [v.code for v in self.violations]


class DeclarationError(DocuconfError):
    """Raised when the settings declaration itself is invalid (SPEC §11.2 item 2)."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems: tuple[str, ...] = tuple(problems)
        lines = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"docuconf: invalid declaration:\n{lines}")


DEFAULT_TERMINATION_LOG = "/dev/termination-log"
#: Kubernetes reads at most 4096 bytes of the termination message.
TERMINATION_LOG_LIMIT = 4096


def write_termination_log(message: str, path: str | None = None) -> None:
    """Write ``message`` to the Kubernetes termination log.

    ``DOCUCONF_TERMINATION_LOG`` (or ``path``) overrides the location and is
    written even if it does not exist yet. The default ``/dev/termination-log``
    is only written when it exists, i.e. inside a container. Best effort: a
    failure to write is ignored, since the error is raised anyway.
    """
    target = path or os.environ.get("DOCUCONF_TERMINATION_LOG") or None
    if target is None:
        if not os.path.exists(DEFAULT_TERMINATION_LOG):
            return
        target = DEFAULT_TERMINATION_LOG
    data = message.encode("utf-8")[:TERMINATION_LOG_LIMIT]
    try:
        with open(target, "wb") as f:
            f.write(data)
    except OSError:
        pass
