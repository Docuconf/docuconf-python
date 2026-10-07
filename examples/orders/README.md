# Example: orders

A tiny service on the standard library's `http.server`, whose configuration is declared with docuconf. It shows
the three things the Python SDK gives an app:

- a normal [pydantic-settings](https://github.com/pydantic/pydantic-settings) `BaseSettings` class, with docuconf
  markers only where pydantic has no word for a rule (URL schemes, comma-separated lists)
  ([`app.py`](app.py));
- one check at boot, `docuconf.load(Settings)`, that reports every problem at once with stable codes;
- a CUE contract exported from the class, for the platform to validate before it deploys
  ([`contract.cue`](contract.cue)).

| Variable | Type | Rules |
|---|---|---|
| `PORT` | int | 1–65535, default `8080` |
| `LOG_LEVEL` | enum | `debug`, `info`, `warn`, `error`; default `info` |
| `DATABASE_URL` | url | secret, required, scheme `postgres` |
| `ALLOWED_ORIGINS` | list of strings, comma-separated | at least 1 item; default `http://localhost:3000` |
| `REQUEST_TIMEOUT` | duration, ISO 8601 (`PT45S`) | `1s`–`5m`, default `30s` |
| `WORKER_COUNT` | int | 1–64, default `4` |

`GET /healthz` returns `ok`; `GET /config` returns the typed values as JSON, with the secret shown as `***`.

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
{"PORT": 8080, "LOG_LEVEL": "info", "DATABASE_URL": "***", "ALLOWED_ORIGINS": ["http://localhost:3000"], "REQUEST_TIMEOUT": "PT30S", "WORKER_COUNT": 4}
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
[`smoke.sh`](smoke.sh) checks both runs (`PYTHON=.venv/bin/python ./smoke.sh`); CI runs it on every push.

## Export the contract

`contract.cue` is generated; never edit it by hand. Re-export it after changing `Settings` (CI fails if it is out of
date):

```console
$ .venv/bin/docuconf export app:Settings -o contract.cue
```

## Deploy

The app ships `contract.cue`, and the platform checks its inputs against it before anything reaches the cluster:
`docuconf vet` reports every bad or missing value, secret given as a literal or policy violation, and
`docuconf render` turns valid inputs into the pod's env, in the encodings this app reads (ISO 8601 for
`REQUEST_TIMEOUT`, so platform authors still write `45s`). A Helm-based platform can use the
[docuconf Helm chart](https://github.com/docuconf/docuconf-go/tree/main/helm) instead, which generates a
`values.schema.json` from the contract.
