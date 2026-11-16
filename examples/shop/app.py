"""The shop app: one image with two roles, web and worker, configured contract-first by docuconf.

It loads ``contract.json`` at startup, prints one line about the role it runs as and exits 0. With a bad
configuration it lists every problem and exits 1, without a traceback and without printing a secret.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

import docuconf
from docuconf import ConfigValidationError, ContractSettings, DeclarationError

CONTRACT = Path(__file__).with_name("contract.json")


def load(env: Mapping[str, str] | None = None) -> ContractSettings:
    """The typed config: ``env`` (default: the process environment) checked against ``contract.json``.

    The checks include the roles (an input of the other role is never required), the ``requiredIf`` conditions
    and the variables of the included ``otel@1`` fragment. Raises :class:`docuconf.ConfigValidationError` listing
    every problem.
    """
    return docuconf.load_contract(CONTRACT, env=env)


def describe(config: ContractSettings) -> str:
    """The line the app prints at startup, from the typed values (the lists are already split)."""
    if config.value("DOCUCONF_ROLE") == "web":
        origins: list[str] = config.value("ALLOWED_ORIGINS")
        return f"shop web: port {config.value('PORT')}, origins {','.join(origins)}"
    queues: list[str] = config.value("QUEUES")
    webhooks = "on" if config.value("WEBHOOKS_ENABLED") else "off"
    return f"shop worker: queues {','.join(queues)}, webhooks {webhooks}"


def main() -> None:
    # As docuconf.load_or_exit does for a settings class: print every problem (the same text goes to the
    # termination log) and exit 1 without a traceback.
    try:
        config = load()
    except (ConfigValidationError, DeclarationError) as e:
        print(e, file=sys.stderr)
        raise SystemExit(1) from None
    print(describe(config))


if __name__ == "__main__":
    main()
