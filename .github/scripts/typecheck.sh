#!/usr/bin/env bash
# Type-checks the fork-owned tree with mypy and the django-stubs plugin, under
# the profile in pyproject.toml. Upstream modules are analysed, not reported.
set -Eeuo pipefail

cd "$(dirname "$0")/../.."
source .github/scripts/fork-owned.sh

# The plugin imports the settings, which refuse to load without a secret key
OPENBENCH_SECRET_KEY="${OPENBENCH_SECRET_KEY:-typecheck-only}" mypy "${fork_owned[@]}"
