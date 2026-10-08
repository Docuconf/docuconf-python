#!/usr/bin/env bash
# Runs only the docuconf-go-facing tests against a given docuconf-go checkout:
# the shared conformance suite (tests/test_conformance.py) and the tests that
# `cue vet` exported contracts against its meta-schema. Not the full suite.
#
#   DOCUCONF_GO_DIR=/path/to/docuconf-go scripts/conformance.sh
#
# Needs Python 3.10+ (PYTHON, default python3) and cue on PATH. Installs the SDK
# into a throwaway virtualenv. docuconf-go's downstream workflow and this
# repository's CI both call it.
set -euo pipefail

: "${DOCUCONF_GO_DIR:?set DOCUCONF_GO_DIR to a docuconf-go checkout}"
DOCUCONF_GO_DIR="$(cd "$DOCUCONF_GO_DIR" && pwd)"
export DOCUCONF_GO_DIR
export DOCUCONF_CONFORMANCE="${DOCUCONF_CONFORMANCE:-$DOCUCONF_GO_DIR/conformance/cases.json}"
export DOCUCONF_SPEC_CUE="${DOCUCONF_SPEC_CUE:-$DOCUCONF_GO_DIR/spec/cue}"
export DOCUCONF_REQUIRE_CONFORMANCE=1
export DOCUCONF_REQUIRE_VET=1

cd "$(dirname "$0")/.."
venv="$(mktemp -d)"
trap 'rm -rf "$venv"' EXIT
"${PYTHON:-python3}" -m venv "$venv"
"$venv/bin/python" -m pip install --quiet --disable-pip-version-check -e ".[tls,yaml,jsonschema]" pytest
"$venv/bin/python" -m pytest -p no:cacheprovider \
  tests/test_conformance.py tests/test_export.py tests/test_overlays.py tests/test_docs.py \
  -k "test_conformance or cue or meta_schema"
