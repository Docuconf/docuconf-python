# Example: orders

A tiny service on the standard library's `http.server`, whose configuration is declared with docuconf. It shows
the three things the Python SDK gives an app:

- a normal [pydantic-settings](https://github.com/pydantic/pydantic-settings) settings class (`DocuconfSettings` is
  a `BaseSettings`), with docuconf markers only where pydantic has no word for a rule (URL schemes,
  comma-separated lists) ([`app.py`](app.py));
- one check at boot, `Settings.load_or_exit()`, that reports every problem at once with stable codes and exits 1
  without a traceback;
- a CUE contract exported from the class, for the platform to validate before it deploys
  ([`contract.cue`](contract.cue)).

| Variable | Type | Rules |
|---|---|---|
| `PORT` | int | 1–65535, default `8080` |
| `LOG_LEVEL` | enum | `debug`, `info`, `warn`, `error`; default `info` |
| `DATABASE_URL` | url | secret, required, scheme `postgres`, at most 2048 characters |
| `ALLOWED_ORIGINS` | list of strings, comma-separated | at least 1 item; default `http://localhost:3000` |
| `REQUEST_TIMEOUT` | duration, ISO 8601 (`PT45S`) | `1s`–`5m`, default `30s` |
| `WORKER_COUNT` | int | 1–64, default `4` |
| `WEBHOOK_KEYS` | key set, comma-separated | secret (always), optional; 1–2 keys of 32–256 characters each |

`GET /healthz` returns `ok`; `GET /config` returns the typed values as JSON, with secrets always shown as `***`, set or
not. `POST /webhooks/payments` accepts a payment webhook signed with any key in `WEBHOOK_KEYS`.

## Run it

[`requirements.txt`](requirements.txt) installs the SDK from this repository (`pip install -e ../..`), not a
published release.

```console
$ cd examples/orders
$ python -m venv .venv && .venv/bin/pip install -r requirements.txt
$ DATABASE_URL=postgres://orders:pw@localhost:5432/orders .venv/bin/python app.py
$ curl localhost:8080/healthz
ok
$ curl localhost:8080/config
{"PORT": 8080, "LOG_LEVEL": "info", "DATABASE_URL": "***", "ALLOWED_ORIGINS": ["http://localhost:3000"], "REQUEST_TIMEOUT": "PT30S", "WORKER_COUNT": 4, "WEBHOOK_KEYS": "***"}
```

## When the configuration is wrong

With `PORT=0` and no `DATABASE_URL`, the service refuses to start, exits 1 and lists every problem, not just the
first:

```console
$ PORT=0 .venv/bin/python app.py
docuconf: 2 configuration problems:
  - DATABASE_URL [missing_required]: required, but not set
  - PORT [out_of_range]: Input should be greater than or equal to 1 (got '0')
```

In Kubernetes the same text goes to `/dev/termination-log`, so `kubectl describe pod` shows it.
[`smoke.sh`](smoke.sh) checks both runs, and the webhook key set below (`PYTHON=.venv/bin/python ./smoke.sh`); CI runs
it on every push.

## Rotate a key

`WEBHOOK_KEYS` is a key set: `POST /webhooks/payments` accepts a body whose `X-Signature` header is the hex
HMAC-SHA256 of the body under any key in the set. It is declared as a `docuconf.KeySet`, which is always secret: the
contract type is `keySet`, and the set prints as `**********`. `verify` in [`app.py`](app.py) checks the signature
with `KeySet.verify`, which tries every key, so the time taken does not say which one matched. A variable is read
once, at start, so a new key reaches the service only when the pods restart; with two keys valid at once, no webhook
is turned away while that happens. The generated docs ([`CONFIG.md`](CONFIG.md)) give the rotation steps: add the new
key (`old,new` in the Secret) and roll out, switch the sender, then remove the old key and roll out.

In the platform's values, the key set is a reference to one Secret key that holds `old,new` while rotating:

```yaml
WEBHOOK_KEYS:
  secretKeyRef: {name: orders-webhooks, key: keys}
```

The contract allows 1 or 2 keys of 32 to 256 characters each, so a trailing comma (an empty key) or a truncated key
stops the service at boot instead of locking out the sender, without printing a key:

```console
$ DATABASE_URL=postgres://orders:pw@localhost:5432/orders \
    WEBHOOK_KEYS=old-webhook-key-0123456789abcdef0123, .venv/bin/python app.py
docuconf: 1 configuration problem:
  - WEBHOOK_KEYS [out_of_range]: key 2 is empty
```

[`test_app.py`](test_app.py) walks through a rotation (`.venv/bin/python -m pytest test_app.py`), and
[`smoke.sh`](smoke.sh) posts webhooks signed with both keys. [docuconf-go's SPEC section
6.1](https://github.com/docuconf/docuconf-go/blob/main/spec/SPEC.md#61-rotation) covers rotation in general.

## Export the contract

`contract.cue` is generated; never edit it by hand. Re-export it after changing `Settings` (CI fails if it is out of
date):

```console
$ .venv/bin/docuconf export app:Settings -o contract.cue
```

## Generated docs

[`CONFIG.md`](CONFIG.md) (for developers), [`CONFIG.agents.md`](CONFIG.agents.md) (for AI agents) and `docs.json` (the
docs model both are rendered from) are generated from `contract.cue` by the `docuconf` CLI from
[docuconf-go](https://github.com/docuconf/docuconf-go); never edit them by hand either. Regenerate them after exporting
the contract (CI runs each with `--check` in place of `-o`, against the committed `contract.cue`):

```console
$ docuconf docs contract.cue -o CONFIG.md
$ docuconf docs contract.cue --format agents -o CONFIG.agents.md
$ docuconf docs contract.cue --format model -o docs.json
```

## Deploy

The app ships `contract.cue`, and the platform checks its inputs against it before anything reaches the cluster:
`docuconf vet` reports every bad or missing value, secret given as a literal or policy violation, and
`docuconf render` turns valid inputs into the pod's env, in the encodings this app reads (ISO 8601 for
`REQUEST_TIMEOUT`, so platform authors still write `45s`). A Helm-based platform can use the
[docuconf Helm chart](https://github.com/docuconf/docuconf-go/tree/main/helm) instead, which generates a
`values.schema.json` from the contract.
