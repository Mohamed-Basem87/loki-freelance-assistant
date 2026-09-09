#!/usr/bin/env bash
# One-command local test bootstrap (audit finding P2-10).
#
# A developer cloning this repository previously had no documented,
# single command that reliably ran the test suite: requirements.txt
# alone does not include pytest, and the exact working `pip install`
# invocation only existed inside .github/workflows/ci.yml. This script
# is that single documented entrypoint -- it is exactly what CI itself
# should be able to delegate to as well, so local and CI test runs
# never drift apart.
#
# Usage:
#   ./scripts/test.sh            # install deps + run the full suite
#   ./scripts/test.sh -k freehub # extra args are passed to pytest
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

python -m pip install --upgrade pip
pip install -r requirements.txt -r requirements-dev.txt

pytest tests/ -q "$@"
