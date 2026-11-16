# Example: shop

A tiny app with no server, database or queue. It shows three parts of the spec that the Python SDK reads in its
[contract-first mode](../../README.md#contract-first-mode) only, since a settings class cannot declare them yet:

- **roles** ([SPEC §4.9](https://github.com/docuconf/docuconf-go/blob/main/spec/SPEC.md)): one image runs as a `web`
  or a `worker` workload, picked by `DOCUCONF_ROLE`. An input marked with the other role is never required;
- **conditional requirements** (`requiredIf`, SPEC §4.2.1): `LOG_SCHEMA_URL` is required when `LOG_FORMAT` is
  `json`, and the webhook signing key file when `WEBHOOKS_ENABLED` is `true`;
- an **included fragment** (`includes: ["otel@1"]`, SPEC §4.8): the OpenTelemetry variables, inline in the contract,
  checked at boot like the app's own.

The contract is written by hand: [`contract.json`](contract.json), which [`app.py`](app.py) loads at startup with
`docuconf.load_contract`, and the same contract in CUE, [`contract.cue`](contract.cue), which the docs are generated
from. CI checks that both pass the meta-schema and that they are the same contract.

| Input | Type | Rules |
|---|---|---|
| `DOCUCONF_ROLE` | enum | `web` or `worker`; required |
| `DATABASE_URL` | url | secret, required, scheme `postgres` or `postgresql` |
| `PORT` | int | `web` only; 1–65535, default `8080` |
| `ALLOWED_ORIGINS` | list of strings, comma-separated | `web` only; required |
| `QUEUES` | list of strings, comma-separated | `worker` only; required |
| `WEBHOOKS_ENABLED` | bool | `worker` only; default `false` |
| `LOG_FORMAT` | enum | `text` or `json`; default `text` |
| `LOG_SCHEMA_URL` | url | scheme `https`; required when `LOG_FORMAT` is `json` |
| `OTEL_*` | from `otel@1` | optional; `OTEL_EXPORTER_OTLP_PROTOCOL` is `grpc`, `http/protobuf` or `http/json` |
| `webhook-signing-key` (file) | text at `/etc/shop/webhooks/signing.key` | `worker` only; secret, at least 32 characters; required when `WEBHOOKS_ENABLED` is `true` |

The full reference is in the generated [`CONFIG.md`](CONFIG.md).

## Run it

[`requirements.txt`](requirements.txt) installs the SDK from this repository (`pip install -e ../..`), not a
published release. The app prints one line about its role and exits 0:

```console
$ cd examples/shop
$ python -m venv .venv && .venv/bin/pip install -r requirements.txt
$ export DATABASE_URL=postgres://shop@db:5432/shop
$ DOCUCONF_ROLE=web ALLOWED_ORIGINS=https://shop.example .venv/bin/python app.py
shop web: port 8080, origins https://shop.example
$ DOCUCONF_ROLE=worker QUEUES=default,mail .venv/bin/python app.py
shop worker: queues default,mail, webhooks off
```

The worker prints its queues from the typed config, where `QUEUES` is already a list.

To turn webhooks on, the worker needs the signing key file. File paths are absolute in the contract; for local
development, `DOCUCONF_FILE_ROOT` names a directory to find them under:

```console
$ mkdir -p dev/etc/shop/webhooks && openssl rand -hex 32 > dev/etc/shop/webhooks/signing.key
$ DOCUCONF_ROLE=worker QUEUES=default WEBHOOKS_ENABLED=true DOCUCONF_FILE_ROOT=dev .venv/bin/python app.py
shop worker: queues default, webhooks on
```

## When the configuration is wrong

The app refuses to start, lists every problem with its code, exits 1 and never prints a secret. The same text goes to
the termination log (`/dev/termination-log`, or `DOCUCONF_TERMINATION_LOG`). A condition names what made the input
required:

```console
$ DOCUCONF_ROLE=worker QUEUES=default LOG_FORMAT=json .venv/bin/python app.py
docuconf: 1 configuration problem:
  - LOG_SCHEMA_URL [missing_required]: is required when LOG_FORMAT is "json"
```

Without a valid role, every input marked with roles is optional, so `DOCUCONF_ROLE` is the one problem reported:

```console
$ DOCUCONF_ROLE=cron .venv/bin/python app.py
docuconf: 1 configuration problem:
  - DOCUCONF_ROLE [not_in_enum]: Input should be 'web' or 'worker' (got 'cron')
```

## The matrix

[`test_app.py`](test_app.py) runs each case (`.venv/bin/python -m pytest test_app.py`), with
`DATABASE_URL=postgres://shop@db:5432/shop` and a fresh, empty `DOCUCONF_FILE_ROOT`. Each failing case has exactly the
one problem shown. Lists are comma-separated (`csv`, the contract's default encoding).

| # | Env | Result |
|---|---|---|
| 1 | `DOCUCONF_ROLE=web ALLOWED_ORIGINS=https://shop.example` | ok: `shop web: port 8080, origins https://shop.example` |
| 2 | `DOCUCONF_ROLE=worker QUEUES=default,mail` | ok: a worker needs no `ALLOWED_ORIGINS` |
| 3 | `DOCUCONF_ROLE=web` | `ALLOWED_ORIGINS` `missing_required` |
| 4 | `QUEUES=default` (no role) | `DOCUCONF_ROLE` `missing_required` |
| 5 | `DOCUCONF_ROLE=cron` | `DOCUCONF_ROLE` `not_in_enum` |
| 6 | `DOCUCONF_ROLE=worker QUEUES=default LOG_FORMAT=json` | `LOG_SCHEMA_URL` `missing_required`: `is required when LOG_FORMAT is "json"` |
| 7 | as 6, plus `LOG_SCHEMA_URL=https://logs.example/v1.json` | ok |
| 8 | `DOCUCONF_ROLE=worker QUEUES=default WEBHOOKS_ENABLED=true`, no key file | `webhook-signing-key` `file_missing`: `..., and is required when WEBHOOKS_ENABLED is true` |
| 9 | as 8, with a key of 32+ characters at `etc/shop/webhooks/signing.key` under the root | ok: `webhooks on` |
| 10 | `DOCUCONF_ROLE=web ALLOWED_ORIGINS=https://shop.example WEBHOOKS_ENABLED=true` | ok: the key file belongs to the worker |
| 11 | `DOCUCONF_ROLE=web ALLOWED_ORIGINS=https://shop.example OTEL_EXPORTER_OTLP_PROTOCOL=carrier-pigeon` | `OTEL_EXPORTER_OTLP_PROTOCOL` `not_in_enum`: fragment variables are checked at boot |

[`smoke.sh`](smoke.sh) runs the app as a process for cases 1, 2 and 6 and checks what it prints and its exit status
(`PYTHON=.venv/bin/python ./smoke.sh`); CI runs it on every push.

## Generated docs

[`CONFIG.md`](CONFIG.md) (for developers), [`CONFIG.agents.md`](CONFIG.agents.md) (for AI agents) and `docs.json` (the
docs model both are rendered from) are generated from `contract.cue` by the `docuconf` CLI from
[docuconf-go](https://github.com/docuconf/docuconf-go); never edit them by hand. Regenerate them after changing the
contract (CI runs each with `--check` in place of `-o`):

```console
$ docuconf docs contract.cue -o CONFIG.md
$ docuconf docs contract.cue --format agents -o CONFIG.agents.md
$ docuconf docs contract.cue --format model -o docs.json
```
