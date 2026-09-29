# Deploying this fork

Settings default to production. Every deployment-specific value comes from the
environment, so no secret or host name lives in the repository.

| Variable | Required | Meaning |
|---|---|---|
| `OPENBENCH_SECRET_KEY` | yes, unless `OPENBENCH_DEBUG` | Django `SECRET_KEY`. |
| `OPENBENCH_DEBUG` | no | `1`/`true`/`yes` enables `DEBUG` and a throwaway secret key. Never in production. |
| `OPENBENCH_ALLOWED_HOSTS` | yes in production | Comma-separated host names. Each non-wildcard entry `h` also becomes the CSRF trusted origin `https://h`. |
| `OPENBENCH_BEHIND_TLS_PROXY` | no | Trust `X-Forwarded-Proto: https` from the reverse proxy and mark session and CSRF cookies `Secure`. Set it only when a TLS proxy that overwrites that header is the sole ingress. |
| `OPENBENCH_DATA_DIR` | no | Directory holding `db.sqlite3`, `Media/`, and the PGN watcher lockfile. Defaults to the checkout. |

## Local development

```bash
OPENBENCH_DEBUG=1 python manage.py migrate
OPENBENCH_DEBUG=1 python manage.py runserver
```

The PGN watcher is started by the WSGI entrypoint (`OpenSite/wsgi.py`), which
both `runserver` and production WSGI servers load, and never by management
commands, which may run before its table exists. A lockfile in
`OPENBENCH_DATA_DIR` keeps it to one process.

## Tests

```bash
OPENBENCH_SECRET_KEY=test python manage.py test OpenBench
```
