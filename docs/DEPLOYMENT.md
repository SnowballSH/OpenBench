# Deploying this fork

Settings default to production. Every deployment-specific value comes from the
environment, so no secret or host name lives in the repository.

| Variable | Required | Meaning |
|---|---|---|
| `OPENBENCH_SECRET_KEY` | yes, unless `OPENBENCH_DEBUG` | Django `SECRET_KEY`. |
| `OPENBENCH_DEBUG` | no | `1`/`true`/`yes` enables `DEBUG` and a throwaway secret key. Never in production. |
| `OPENBENCH_ALLOWED_HOSTS` | yes in production | Comma-separated host names. Each non-wildcard entry `h` also becomes the CSRF trusted origin `https://h`. |
| `OPENBENCH_BEHIND_TLS_PROXY` | no | Trust `X-Forwarded-Proto: https` from the reverse proxy and mark session and CSRF cookies `Secure`. Set it only when a TLS proxy that overwrites that header is the sole ingress. It also makes the login throttle take the client address from the right-most `X-Forwarded-For` entry (see [SECURITY.md](SECURITY.md)). |
| `OPENBENCH_SECRET_KEY_FILE` | no | Path of a file holding the key; takes precedence over `OPENBENCH_SECRET_KEY`. |
| `OPENBENCH_DATA_DIR` | no | Directory holding `db.sqlite3`, `Media/`, and the PGN watcher lockfile. Defaults to the checkout. |
| `OPENBENCH_UPLOAD_TEMP_DIR` | no | Where uploads larger than 2.5 MiB spool while they arrive. Defaults to the system temporary directory. |
| `OPENBENCH_WORKERS`, `OPENBENCH_THREADS` | no | gunicorn processes and threads per process in the container image. Default 2 and 4. |

## Local development

```bash
OPENBENCH_DEBUG=1 python manage.py migrate
OPENBENCH_DEBUG=1 python manage.py seed_demo
OPENBENCH_DEBUG=1 python manage.py runserver
```

`seed_demo` fills an empty development database with accounts, Machines (one
of them a supervised host with a registration per workload and idle ones in
between, and a few ephemeral `batch-<uuid>` cloud jobs) and Workloads of every mode (SPRT, fixed games, SPSA tunes, datagen) in every
state, so pages can be seen with realistic data. It refuses
to run without `OPENBENCH_DEBUG`, and prints the demo login it created.

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
login; methods other than `GET` and `HEAD` get 405. Host validation still applies, so a local health checker needs its
address in `OPENBENCH_ALLOWED_HOSTS` (for example
`openbench.example.com,127.0.0.1`) or must send the public `Host` header.

Each gunicorn worker tries once, at start, to take the PGN watcher lockfile,
so exactly one runs the watcher. The lock dies with its process, and gunicorn
replaces an exited worker with a new one that takes it over.

## Storage

`/manage/storage/` shows what fills the data directory. Only managers (a
Profile or Django superuser) see it, the same rule as `/api/storage/`, because
it reveals filesystem totals and Media file names: anonymous visitors are sent
to the login page, accounts not yet enabled back to the index, and other users
to `/manage/books/`. It has no forms and changes nothing.

| Section | Contents |
|---|---|
| Tiles | Free space on the filesystem holding `Media/`, or `OPENBENCH_DATA_DIR` when `Media/` does not exist yet (warning edge and a banner when under 2 GiB or 15% free), the database (`db.sqlite3` plus its `-wal` and `-shm` files), everything under `Media/`, and the network files. |
| Media by category | Files and bytes per category, with each category's share of `Media/`. |
| Networks by engine | Networks, distinct files, rows whose file is missing, and bytes, linked to that engine's Networks page (unlinked when the Engine is no longer configured). Each network's file is checked directly with `lstat`, so this table stays exact when the Media scan is cut short. A file shared by several engines counts once per engine here and once in the totals. |
| Largest items | The ten largest files per category, linked to the owning network list, workload or event. |

Categories come from the file layout in `Media/`:

| Category | Files |
|---|---|
| Networks | `<SHA>` at the top level, the first eight hex digits of a network's sha256, named by a `Network` row. |
| Archived PGNs | `PGNs/<workload>.pgn.tar`, appended to by the PGN watcher. |
| Pending PGN batches | `<workload>.<result>.<book index>.pgn.bz2`, uploads the watcher has not archived yet. |
| Event logs | `event<id>.log`, a Client error's log. |
| Other | Anything else. A top-level eight-hex-digit file no `Network` names is flagged; a directory other than `PGNs/` is one item with the sum of its files. |

The upload spool (`OPENBENCH_UPLOAD_TEMP_DIR`, `/data/upload-tmp` in the
container) is reported beside the tiles, walked with its own limit of 10,000
entries. Symbolic links are never followed, so nothing outside `Media/` is
counted; the page says how many it skipped at any depth, and how many
directories it could not read. The scan reads directory entries and `stat`
results only, never file contents, stops after 100,000 entries (the page then
says the totals are partial), and is cached for 60 seconds per process.

### Freeing space safely

- **Networks**: delete retired networks from the engine's Networks page (or
  `POST /api/networks/<engine>/<name>/delete/`), as an Approver. Default and
  previous default networks cannot be deleted. The file goes once no network of
  any engine uses it, and a workload still naming it can no longer fetch it.
- **Archived PGNs**: only for finished workloads, since the watcher keeps
  appending to an active one's archive. Download `/api/pgns/<workload>/` first
  if the games are wanted, then remove `Media/PGNs/<workload>.pgn.tar` on the
  host. Nothing else refers to the file; the download then reports it missing.
- **Pending PGN batches**: leave them. The watcher archives and removes them;
  a growing count means it is not running (see the lockfile note above).
- **Event logs**: an `event<id>.log` can be removed on the host once no one
  needs it; the event row stays, and opening it reports that no logs exist.
- **Database**: the `-wal` file shrinks at checkpoints. Reclaiming space from
  deleted rows needs `VACUUM`, which takes the write lock and temporarily needs
  free space equal to the database, so run it in a quiet period after a backup.
- **Upload spool**: files there belong to uploads in progress. A file left by a
  crashed worker can be removed once no upload is running.

Never delete from `Media/` by pattern; remove named files only.

### `GET /api/storage/`

Needs an enabled user: a browser session, or `username` and `password` in a
POST body like the other API endpoints. Otherwise it answers 401 with
`{"error": ...}`. Sizes are bytes.

```json
{
    "storage": {
        "generated_at": "2026-09-30T12:00:00+00:00",
        "cache_seconds": 60,
        "disk": {
            "total_bytes": 32212254720,
            "used_bytes": 26843545600,
            "free_bytes": 5368709120,
            "free_fraction": 0.1667,
            "low": false
        },
        "database": { "bytes": 52428800, "main_bytes": 50331648, "wal_bytes": 2064384, "shm_bytes": 32768 },
        "media": {
            "files": 412,
            "bytes": 1610612736,
            "skipped_symlinks": 0,
            "unreadable_dirs": 0,
            "truncated": false,
            "categories": [
                {
                    "key": "networks",
                    "label": "Networks",
                    "files": 12,
                    "bytes": 402653184,
                    "largest": [
                        { "name": "0A1B2C3D", "bytes": 33554432, "detail": "Avalanche / net-42", "url": "/networks/Avalanche/" }
                    ]
                }
            ]
        },
        "networks_by_engine": [
            { "engine": "Avalanche", "networks": 12, "files": 12, "missing_files": 0, "bytes": 402653184, "url": "/networks/Avalanche/" }
        ],
        "upload_spool": { "files": 0, "bytes": 0, "truncated": false }
    }
}
```

| Field | Meaning |
|---|---|
| `disk` | Measured on `Media/`, or on `OPENBENCH_DATA_DIR` when `Media/` is missing; `null` when neither can be measured. `low` is true under 2 GiB or 15% free. |
| `media.categories` | Always all five, in order: `networks`, `pgn_archives`, `pgn_pending`, `event_logs`, `other`. `largest` holds up to ten items, biggest first. |
| `largest[].url`, `networks_by_engine[].url` | The owning page, or `null` when nothing owns the file (a deleted workload, an Engine no longer configured, an unreferenced log or network file, anything in `other`). |
| `networks_by_engine[].files`, `missing_files`, `bytes` | From an `lstat` of each network's file, independent of the Media scan limit. |
| `media.skipped_symlinks`, `media.unreadable_dirs` | Symbolic links not followed, and directories that could not be listed (their contents are not counted). |
| `media.truncated` | The scan hit its entry limit, so totals are low. |
| `upload_spool` | `null` when `OPENBENCH_UPLOAD_TEMP_DIR` is unset. `truncated` is true when it holds more than 10,000 entries, so the figures are low. |

## Machine registrations

The Client registers a new `Machine` row on every start. A supervisor running
it with `--single-workload` starts it once a minute while there is no work, so
an idle host would add about 1,440 rows a day that never play a game. Rows
with Results are history and are never removed (`Result.machine` is
`PROTECT`); the never-used ones are removed in two ways.

**At registration**, `clientWorkerInfo` deletes, in the same transaction as
the new row, registrations that are all of:

- of the same owner as the one registering (not the same machine: an
  ephemeral cloud job is a new host every time and never registers again, so
  only another of its owner's hosts can clean up after it),
- without any Result,
- made by a Client that exits when it has no work (`--single-workload` or
  `--fleet` in the `cli_options` it registered with),
- last heard from more than 15 minutes ago,

looking only at the owner's 500 most recently seen registrations older than
15 minutes. Such a registration made one workload request and its Client has
exited: it cannot come back, whichever machine it ran on. The 15 minutes cover
a Client that registered and is still retrying its first request through a
network outage. The 500-row window makes the cost constant: one query down the
`machine_user_updated` index (2 ms for an owner with 45,000 registrations) and
one delete. In steady state every never-used row sits inside the window, so an
owner keeps roughly one row per idle host per minute for 15 minutes, plus the
registrations that played. A backlog deeper than the window (never-used rows
buried under more than 500 newer ones) is left for `prune_machines`. Reusing
the newest idle row instead of inserting was rejected: it would change the id
and secret handed to a Client that may still be starting, and would never
shrink a backlog.

Registrations of a Client that keeps polling are deliberately left alone at
registration. `clientGetWorkload` does not refresh `Machine.updated` when it
has no work to give, so an idle polling Client is indistinguishable from a dead
one by its heartbeat. If its row is deleted, its next request is answered
`Bad Client Version: Bad Machine Id`; the Client re-downloads itself and
registers again, but one started with `--no-client-downloads` exits instead.

The tidy-up runs after the new registration is saved, in its own transaction,
and is best effort: a database error in it is logged
(`OpenBench.fleet.housekeeping`) and the Client still gets its id and secret.

**On demand**, `prune_machines` reports, and with `--apply` deletes, across
the whole server:

```bash
python manage.py prune_machines                              # dry run: counts only
python manage.py prune_machines --apply                      # delete exited registrations
python manage.py prune_machines --apply --include-polling --days 30
```

- *exited*: as above, for every owner and without the window; this is what
  clears a backlog, and all that a plain `--apply` deletes.
- *polling*, only with `--include-polling`: never-used registrations of
  Clients that keep polling, last heard from more than `--days` days ago
  (default 7, minimum 1). Mind the caveat above: a polling Client idle for
  that long loses its registration and registers again, or exits under
  `--no-client-downloads`. Raise `--days` beyond the longest quiet period.

It never deletes a registration that has a Result or a heartbeat within the
last 15 minutes, deletes in batches of 500, re-checks for Results at delete
time, and reports how many it actually deleted. `--apply` also gives a host
key to every registration that lacks one. Deleting a registration does not
change any count of games, and LogEvents that name its id keep their text.
Take a database backup before the first `--apply`.

### Rolling back past the host key

Migration `0019` adds `Machine.host_key` and two indexes. Rolling the image
back to one from before it does not undo the migration, and does not need to:

- The column is `NOT NULL DEFAULT ''` in the database, so the older code,
  which does not know the column, still inserts Machines; workers keep
  registering. Those rows have an empty key.
- The indexes are invisible to older code.
- Do not run `migrate OpenBench 0018` to "match" the old image: it would drop
  the column and every key, for no benefit.

When the newer image runs again, nothing has to be done by hand. Rows with an
empty key are never merged with each other, and are keyed by the first
registration that arrives (200 per registration), by the row's own next
heartbeat, and by the fleet pages when they meet one. `prune_machines --apply`
keys all of them at once; the dry run reports how many there are.

## Response compression

The server sends HTML as rendered, without minifying it. Upstream used
django-htmlmin, which re-parses every response with html5lib: on a busy index
or search page that parse cost more than the whole view and its queries, while
saving only about a fifth of the bytes. Compression belongs to the reverse
proxy (for Caddy, `encode zstd gzip` on the vhost), which shrinks the same
pages far more for a fraction of the CPU, and also covers the JSON endpoints.
Enable it at the proxy when deploying: without it, pages are sent
uncompressed and about a quarter larger than htmlmin made them. Django masks
the CSRF token in every response, so compressing pages that reflect query
parameters does not expose it to BREACH-style guessing.
