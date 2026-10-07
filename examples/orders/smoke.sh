#!/usr/bin/env bash
# Smoke test: starts the orders service with valid env and checks its
# endpoints, then starts it with bad env and checks it refuses to boot.
# Needs curl, and a Python with requirements.txt installed ($PYTHON, default python3).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
python="${PYTHON:-python3}"
tmp="$(mktemp -d)"
pid=""
trap '[ -n "$pid" ] && kill "$pid" 2>/dev/null; rm -rf "$tmp"' EXIT
cd "$here"

secret='postgres://orders:s3cr3t-pw@localhost:5432/orders'
# A free port, so the test never talks to another server.
port="$("$python" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"

# 1. Valid env: the service serves /healthz and /config, without the secret.
PORT=$port DATABASE_URL="$secret" "$python" app.py >"$tmp/out.txt" 2>&1 &
pid=$!
for _ in $(seq 50); do
  kill -0 "$pid" 2>/dev/null || { echo "the service exited:" >&2; cat "$tmp/out.txt" >&2; exit 1; }
  curl -fsS "http://127.0.0.1:$port/healthz" >"$tmp/healthz" 2>/dev/null && break
  sleep 0.1
done
[ "$(cat "$tmp/healthz" 2>/dev/null)" = ok ] || { echo "GET /healthz did not return ok" >&2; cat "$tmp/out.txt" >&2; exit 1; }
curl -fsS "http://127.0.0.1:$port/config" >"$tmp/config.json"
if grep -q 's3cr3t-pw' "$tmp/config.json"; then
  echo "GET /config leaked the secret" >&2; exit 1
fi
grep -q '"DATABASE_URL": "\*\*\*"' "$tmp/config.json" || { echo "GET /config did not redact DATABASE_URL" >&2; exit 1; }
echo "valid env: /healthz ok, /config $(cat "$tmp/config.json")"
kill "$pid"; wait "$pid" 2>/dev/null || true; pid=""

# 2. PORT=0 and no DATABASE_URL: the service exits non-zero and names both.
if env -u DATABASE_URL PORT=0 "$python" app.py >"$tmp/bad.txt" 2>&1; then
  echo "service started with PORT=0 and no DATABASE_URL" >&2; exit 1
fi
for code in missing_required out_of_range; do
  grep -q "$code" "$tmp/bad.txt" || { echo "startup output lacks $code:" >&2; cat "$tmp/bad.txt" >&2; exit 1; }
done
echo "bad env: exited non-zero with:"
sed 's/^/  /' "$tmp/bad.txt"
echo "smoke: ok"
