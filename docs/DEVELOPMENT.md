# Developing this fork

The server targets Python 3.14 and Django 5.2. Install the dependencies with
`pip install -r requirements.txt -r Client/requirements.txt`.

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
version refuses to run. Locally:

```bash
uv tool install ruff@0.16.9
.github/scripts/lint.sh
ruff format OpenBench/insights OpenBench/security \
    OpenBench/management/commands/seed_demo.py OpenBench/tests
```

The script is the one place that lists the fork-owned paths. When you add a
fork-owned module outside them, add it there.

## Fork-owned and upstream-owned code

This is a fork of [AndyGrant/OpenBench](https://github.com/AndyGrant/OpenBench),
and upstream changes are merged in periodically.

- **Fork-owned**: `OpenBench/insights/`, `OpenBench/security/`,
  `OpenBench/management/commands/seed_demo.py` and `OpenBench/tests/`. These
  are held to the full rule set in `pyproject.toml` and to `ruff format`.
- **Upstream-owned**: everything else, including `OpenBench/views.py`,
  `OpenBench/utils.py`, `OpenBench/workloads/`, `OpenBench/templatetags/`,
  `Client/` and `Scripts/`. Restyling these would turn every upstream merge
  into a conflict, so they keep upstream's style and receive only the edits a
  feature needs. They are checked for undefined names, redefinitions and
  similar bugs only (`F821`, `F811`, `F632`, `F704`, `F706`, `F823`).

## Feature notes

- [INSIGHTS.md](INSIGHTS.md): workload progress, throughput and strength
  statistics.
- [SECURITY.md](SECURITY.md): operator-visible security behaviour.
- [UI.md](UI.md): template and stylesheet conventions.
- [SUPERVISED_WORKERS.md](SUPERVISED_WORKERS.md): sharing a machine with an
  external supervisor.
- [DEPLOYMENT.md](DEPLOYMENT.md): settings, container image and production
  deployment.
