#!/usr/bin/env bash
# Holds the fork-owned tree to the full ruff rule set and format, and holds
# the whole server, upstream code included, only to the bug-catching rules.
set -Eeuo pipefail

cd "$(dirname "$0")/../.."

fork_owned=(
    OpenBench/fleet
    OpenBench/insights
    OpenBench/progress
    OpenBench/security
    OpenBench/storage
    OpenBench/page_queries.py
    OpenBench/workloads/clone.py
    OpenBench/management/commands/seed_demo.py
    OpenBench/tests
)
server=(OpenBench OpenSite manage.py)
safety_net=F821,F811,F632,F704,F706,F823

ruff check "${fork_owned[@]}"
ruff format --check --diff "${fork_owned[@]}"
ruff check --select "${safety_net}" "${server[@]}"
