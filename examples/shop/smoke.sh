#!/usr/bin/env bash
# Smoke test: runs the shop app as a real process for the web role, the worker
# role and a worker whose json logs lack their schema URL (matrix cases 1, 2
# and 6), and checks what it prints and its exit status.
# Needs a Python with requirements.txt installed ($PYTHON, default python3).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
python="${PYTHON:-python3}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$here"

secret='postgres://shop:s3cr3t-pw@db:5432/shop'

# run NAME WANT_STATUS VAR=VALUE...: runs app.py with only the given env (and a
# fresh, empty file root), and leaves its stdout and stderr in $tmp/NAME.out and .err.
run() {
  local name="$1" want="$2" status=0
  shift 2
  mkdir "$tmp/$name.root"
  env -i PATH="$PATH" DATABASE_URL="$secret" DOCUCONF_FILE_ROOT="$tmp/$name.root" "$@" \
    "$python" app.py >"$tmp/$name.out" 2>"$tmp/$name.err" || status=$?
  if [ "$status" != "$want" ]; then
    echo "$name: want exit status $want, got $status" >&2; cat "$tmp/$name.out" "$tmp/$name.err" >&2; exit 1
  fi
  if grep -q s3cr3t-pw "$tmp/$name.out" "$tmp/$name.err"; then echo "$name: printed the DATABASE_URL" >&2; exit 1; fi
}

# expect NAME FILE: the file holds exactly stdin.
expect() {
  if ! diff -u - "$2"; then echo "$1: unexpected output" >&2; exit 1; fi
}

# 1. The web role.
run web 0 DOCUCONF_ROLE=web ALLOWED_ORIGINS=https://shop.example
echo 'shop web: port 8080, origins https://shop.example' | expect web "$tmp/web.out"
echo "web: $(cat "$tmp/web.out")"

# 2. The worker role, which needs no ALLOWED_ORIGINS.
run worker 0 DOCUCONF_ROLE=worker QUEUES=default,mail
echo 'shop worker: queues default,mail, webhooks off' | expect worker "$tmp/worker.out"
echo "worker: $(cat "$tmp/worker.out")"

# 6. json logs without LOG_SCHEMA_URL: exit 1, naming the condition, with no traceback.
run json-logs 1 DOCUCONF_ROLE=worker QUEUES=default LOG_FORMAT=json
expect json-logs "$tmp/json-logs.out" </dev/null
expect json-logs "$tmp/json-logs.err" <<'WANT'
docuconf: 1 configuration problem:
  - LOG_SCHEMA_URL [missing_required]: is required when LOG_FORMAT is "json"
WANT
echo "json logs without a schema URL: exited 1"
echo "smoke: ok"
