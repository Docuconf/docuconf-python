#!/usr/bin/env bash
# Checks that generated contracts match what is committed, ignoring only the
# value of metadata.generator.version. That value is the SDK version, which
# every release PR bumps; everything else must match exactly.
#
#   scripts/check-generated.sh PATH...       working tree against the index (like git diff --exit-code)
#   scripts/check-generated.sh --files A B   two files against each other (like diff -u)
set -euo pipefail

normalize() {
  # "generator": {... "version": "x" ...} in JSON, generator: {... version: "x" ...} in CUE.
  perl -0pe 's/("?generator"?\s*:\s*\{[^{}]*?\bversion"?\s*:\s*)"[^"]*"/$1"<generator-version>"/g'
}

if [ "${1:-}" = "--files" ]; then
  [ $# -eq 3 ] || { echo "usage: $0 --files COMMITTED FRESH" >&2; exit 2; }
  diff -u --label "$2" --label "$3" <(normalize <"$2") <(normalize <"$3")
  exit
fi

[ $# -gt 0 ] || { echo "usage: $0 PATH..." >&2; exit 2; }
changed=()
while IFS= read -r -d '' f; do changed+=("$f"); done < <(git diff -z --name-only -- "$@")
cd "$(git rev-parse --show-toplevel)"
status=0
for f in ${changed[@]+"${changed[@]}"}; do
  if [ ! -f "$f" ]; then
    echo "$f: deleted" >&2
    status=1
  elif ! diff -u --label "a/$f" --label "b/$f" <(git show ":$f" | normalize) <(normalize <"$f"); then
    status=1
  fi
done
exit "$status"
