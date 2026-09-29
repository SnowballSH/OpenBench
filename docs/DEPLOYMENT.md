# Deploying this fork

Settings default to production. Every deployment-specific value comes from the
environment, so no secret or host name lives in the repository.

| Variable | Required | Meaning |
|---|---|---|
| `OPENBENCH_SECRET_KEY` | yes, unless `OPENBENCH_DEBUG` | Django `SECRET_KEY`. |
| `OPENBENCH_DEBUG` | no | `1`/`true`/`yes` enables `DEBUG` and a throwaway secret key. Never in production. |
| `OPENBENCH_ALLOWED_HOSTS` | yes in production | Comma-separated host names. Each non-wildcard entry `h` also becomes the CSRF trusted origin `https://h`. |
| `OPENBENCH_BEHIND_TLS_PROXY` | no | Trust `X-Forwarded-Proto: https` from the reverse proxy and mark session and CSRF cookies `Secure`. Set it only when a TLS proxy that overwrites that header is the sole ingress. |
| `OPENBENCH_SECRET_KEY_FILE` | no | Path of a file holding the key; takes precedence over `OPENBENCH_SECRET_KEY`. |
| `OPENBENCH_DATA_DIR` | no | Directory holding `db.sqlite3`, `Media/`, and the PGN watcher lockfile. Defaults to the checkout. |
| `OPENBENCH_UPLOAD_TEMP_DIR` | no | Where uploads larger than 2.5 MiB spool while they arrive. Defaults to the system temporary directory. |
| `OPENBENCH_WORKERS`, `OPENBENCH_THREADS` | no | gunicorn processes and threads per process in the container image. Default 2 and 4. |

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

## Container image

`Containerfile` builds the image CI publishes as `ghcr.io/snowballsh/openbench`
(tags `sha-<commit>` and `latest`, from `master` only). It runs as uid 10001
with a read-only root: `/data` holds all state (`OPENBENCH_DATA_DIR`), uploads
spool to `/data/upload-tmp`, and `/tmp` only needs a small tmpfs for gunicorn's
heartbeat files. The entrypoint runs `migrate`, then `gunicorn` with threaded
workers and no control socket. Static files are collected at build time and
served by WhiteNoise.

`/data` must be writable by uid 10001 inside the container. Under rootless
podman, either map the host owner onto it (`UserNS=keep-id:uid=10001,gid=10001`)
or chown the directory inside the user namespace (`podman unshare chown
10001:10001 <dir>`).

SQLite runs in WAL mode with `IMMEDIATE` transactions, so concurrent workers
queue for the write lock for up to 20 seconds instead of failing with
`database is locked`. Back it up with SQLite's online backup API, never by
copying `db.sqlite3` alone.

`GET /health/` answers 200 when the database does and 503 otherwise, without a
login. Host validation still applies, so a local health checker needs its
address in `OPENBENCH_ALLOWED_HOSTS` (for example
`openbench.example.com,127.0.0.1`) or must send the public `Host` header.

Each gunicorn worker tries once, at start, to take the PGN watcher lockfile,
so exactly one runs the watcher. The lock dies with its process, and gunicorn
replaces an exited worker with a new one that takes it over.
