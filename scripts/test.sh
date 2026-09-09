#!/usr/bin/env bash
# One-command local test bootstrap (audit finding P2-10).
#
# A developer cloning this repository previously had no documented,
# single command that reliably ran the test suite: requirements.txt
# alone did not include pytest, and the exact working `pip install`
# invocation only existed inside .github/workflows/ci.yml. pytest is now
# folded into requirements.txt, but this script remains the single
# documented entrypoint -- it is exactly what CI itself delegates to, so
# local and CI test runs never drift apart.
#
# Usage:
#   ./scripts/test.sh            # install deps + run the full suite
#   ./scripts/test.sh -k freehub # extra args are passed to pytest
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

python -m pip install --upgrade pip
pip install -r requirements.txt

pytest tests/ -q "$@"
