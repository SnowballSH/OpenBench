# Developing this fork

The server targets Python 3.14 and Django 5.2. Install the dependencies with
`pip install -r requirements.txt -r Client/requirements.txt`.

`requirements.in` lists the server's direct dependencies; `requirements.txt`
is compiled from it with every transitive version pinned, so the container
image builds the same way every time. Change a dependency in
`requirements.in`, then recompile, and upgrade everything within its bounds
with `--upgrade`:

```bash
uv pip compile requirements.in -o requirements.txt --python-version 3.14 --no-header
```

Dependabot opens a weekly grouped pull request for the workflow actions, which
are pinned by commit SHA. It does not manage Python dependencies, because it
would edit `requirements.txt` without recompiling it; recompile with
`--upgrade` instead.

## Local data

[DEPLOYMENT.md](DEPLOYMENT.md#local-development) shows how to migrate, run
`seed_demo` for accounts, Machines and Workloads in every state, and start the
development server.

## Tests

```bash
OPENBENCH_SECRET_KEY=test python manage.py test OpenBench
```

## Lint and format

CI runs [`.github/scripts/lint.sh`](../.github/scripts/lint.sh) with ruff at
the version pinned by `required-version` in `pyproject.toml`; any other ruff
version refuses to run. The script runs three checks:

1. `ruff check` with the full rule set in `pyproject.toml` over the
   fork-owned paths.
2. `ruff format --check --diff` over the same paths.
3. `ruff check` with only the bug-catching rules (`F821`, `F811`, `F632`,
   `F704`, `F706`, `F823`) over the whole server: `OpenBench/`, `OpenSite/`
   and `manage.py`. Upstream modules that use `from OpenBench.models import *`
   (`views.py`, `utils.py`, `model_utils.py` and four modules under
   `workloads/`) are outside the undefined-name check: ruff cannot tell a
   star-imported name from a missing one there, and the rule that would flag
   such names (`F405`) fires on every existing model reference. Their tests
   are the safety net for those files.

Run it locally without installing ruff, or with ruff 0.16.9 installed:

```bash
uvx --from ruff==0.16.9 bash .github/scripts/lint.sh
.github/scripts/lint.sh
```

To fix findings, run `ruff check --fix` and `ruff format` on the fork-owned
files you changed; never on upstream-owned files.

The script is the one place that lists the fork-owned paths. When you add a
fork-owned module outside them, add it there and to the list below.

## Fork-owned and upstream-owned code

This is a fork of [AndyGrant/OpenBench](https://github.com/AndyGrant/OpenBench),
and upstream changes are merged in periodically.

- **Fork-owned**: `OpenBench/fleet/`, `OpenBench/insights/`,
  `OpenBench/progress/`, `OpenBench/security/`, `OpenBench/storage/`,
  `OpenBench/page_queries.py`, `OpenBench/workloads/clone.py`,
  `OpenBench/management/commands/seed_demo.py` and `OpenBench/tests/`. These
  are held to the full rule set in `pyproject.toml` and to `ruff format`.
- **Upstream-owned**: everything else, including `OpenBench/views.py`,
  `OpenBench/utils.py`, `OpenBench/workloads/` apart from `clone.py`,
  `OpenBench/templatetags/`, `Client/` and `Scripts/`. Restyling these would
  turn every upstream merge into a conflict, so they keep upstream's style and
  receive only the edits a feature needs. They are checked by the
  bug-catching rules only.

## Feature notes

- [API.md](API.md): the JSON API, the `/scripts/` endpoint and `/health/`.
- [WORKLOADS.md](WORKLOADS.md): creating tests, tunes and datagen sessions.
- [PERFORMANCE.md](PERFORMANCE.md): per-page query budgets and the tests that
  pin them.
- [INSIGHTS.md](INSIGHTS.md): workload progress, throughput and strength
  statistics.
- [SECURITY.md](SECURITY.md): operator-visible security behaviour.
- [UI.md](UI.md): template and stylesheet conventions.
- [SUPERVISED_WORKERS.md](SUPERVISED_WORKERS.md): sharing a machine with an
  external supervisor.
- [DEPLOYMENT.md](DEPLOYMENT.md): settings, container image and production
  deployment.
