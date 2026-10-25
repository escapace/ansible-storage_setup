#!/usr/bin/env bash
set -euo pipefail

project_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Do not inherit an activated Ansible environment.
unset VIRTUAL_ENV
cd -- "${project_directory}"

# Isolated environments stay outside the role payload and filename-lint scope.
uv run --isolated --locked ruff check
uv run --isolated --locked ruff format --check
uv run --isolated --locked ty check --error-on-warning
uv run --isolated --locked python -W error tests/storage-setup.py
