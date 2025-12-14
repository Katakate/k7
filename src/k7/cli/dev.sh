#!/usr/bin/env bash
set -euo pipefail
# Ensure we run from repo root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
cd "$REPO_ROOT"
# Run as module to avoid circular import (Python adds script dir to sys.path when running directly)
PYTHONPATH=src exec uv run --with typer --with kubernetes --with python-dotenv --with pyyaml --with rich python3 -m k7.cli.k7 "$@"