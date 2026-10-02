# HTTP API

Every endpoint a script or dashboard can call on this fork: the JSON API under
`/api/`, the `/scripts/` form endpoint that `Scripts/*.py` use to upload
networks and create tests, and `/health/`. Routes are in `OpenBench/urls.py`;
the views are in `OpenBench/views.py` and in the `views.py` of the fork's
packages (`insights`, `storage`, `progress`, `diagnosis`, `triage`, `digest`
and the others).

The examples below were captured from a local server filled by
`manage.py seed_demo`. Long arrays are trimmed, and marked as trimmed.

## Contents

- [Conventions](#conventions)
- [Authentication](#authentication)
- [Endpoint summary](#endpoint-summary)
- [Configuration](#configuration): `api/config/`, `api/config/<engine>/`, `api/buildinfo/`
- [Networks](#networks): list, download, delete
- [Workloads](#workloads): `api/workloads/`, `api/live/…`, `api/workload/<id>/<query>/`, `api/spsa/<id>/<query>/`, `api/pgns/<id>/`
- [Server](#server): `api/insights/server/`, `api/progress/`, `api/digest/`, `api/errors/`, `api/jump/`, `api/storage/`, `api/active/`
- [`/scripts/`](#scripts): upload a network, create a test
- [`/health/`](#health)
- [Client worker endpoints](#client-worker-endpoints)
- [Scripting](#scripting)

## Conventions

- Every path ends in `/`. Django's `APPEND_SLASH` redirects a `GET` without the
  slash, but not a `POST`: that is a 500 with `OPENBENCH_DEBUG` on, and
  otherwise a 301 that the client follows as a `GET` without the body. Always
  send the slash.
- JSON bodies are indented with four spaces. Content type is
  `application/json`.
- Errors are `{"error": "<message>"}` with a status that says what went wrong:

  | Status | Meaning |
  |---|---|
  | 400 | Malformed request fields or parameters (`api/active/`, `api/workloads/`, `api/progress/`, `api/digest/`) |
  | 401 | No usable credentials: none sent, wrong password, or account not enabled |
  | 403 | Authenticated but not permitted (a non-Approver deleting a network), or a CSRF or cross-site refusal |
  | 404 | Unknown engine, network, workload, query or PGN archive |
  | 405 | Wrong method |
  | 409 | Refused because of the resource's state: deleting a default network, or a PGN archive that is not ready |
  | 429 | Throttled after failed logins |

  Upstream OpenBench answers most of these with status 200; this fork does
  not. `/scripts/` is the exception: it is a form endpoint and reports through
  redirects and HTML banners.
- The Host header must be one of `OPENBENCH_ALLOWED_HOSTS`; any other host
  gets Django's 400.

## Authentication

### Two ways to authenticate

1. **Credentials in the POST body**: form fields `username` and `password`
   (`application/x-www-form-urlencoded` or `multipart/form-data`). They are
   read from the POST body only, never from the query string, headers or
   cookies, so a script must `POST` even to read-only endpoints.
2. **A logged-in browser session**: the `sessionid` cookie set by `/login/`,
   `/scripts/` or `/clientGetNetwork/`. With a session, `GET` works too.

A session takes precedence: when the request carries a logged-in session, the
POSTed credentials are ignored (`api_user` in `OpenBench/views.py`).

In both cases the account's Profile must be **enabled**. A correct password
for an account that is not enabled is treated like a wrong one.

### Which endpoints need a login

`Config/config.json` sets `require_login_to_view`, and this fork ships it as
`true`. Most read endpoints use `api_authenticate`, which demands an enabled
user only when that flag is set; the rest always demand one.

| Rule | Endpoints |
|---|---|
| Enabled user when `require_login_to_view` is `true`, otherwise public | `api/config/`, `api/config/<engine>/`, `api/buildinfo/`, `api/networks/<engine>/`, `api/workload/…`, `api/live/…`, `api/spsa/…`, `api/pgns/<id>/`, `api/insights/server/`, `api/errors/` |
| Always an enabled user | `api/active/`, `api/networks/<engine>/<id>/` (download), `/scripts/` |
| Always an enabled **Approver** | `api/networks/<engine>/<id>/delete/`, and `UPLOAD_NETWORK` through `/scripts/` |
| Always a **manager** (Profile or Django superuser) | `api/storage/` |
| None | `/health/` |

When `require_login_to_view` is `false`, the public endpoints in the first row
never check credentials at all, so a wrong password there is not an error and
is not counted by the throttle.

### Failed authentication

Every `/api/` endpoint answers a failed login with 401. The message differs:

| Endpoints | Body |
|---|---|
| `api/config/…`, `api/buildinfo/`, `api/networks/<engine>/`, `api/workload/…`, `api/live/…`, `api/spsa/…`, `api/pgns/…`, `api/insights/server/`, `api/errors/` | `{"error": "API requires authentication for this server"}` |
| `api/networks/<engine>/<id>/` (download), `api/networks/<engine>/<id>/delete/`, `api/storage/` | `{"error": "API requires authentication for this endpoint"}` |
| `api/active/` | `{"error": "Bad Credentials"}` |

An enabled user who is not an Approver gets 403
`{"error": "Only Approvers may delete Networks"}` from the delete endpoint.
`/scripts/` redirects to `/login/` with "Unable to authenticate user" in the
session banner.

The Client only calls the network download endpoint. It writes whatever body
comes back to the network file and then checks its SHA, so an error fails the
same way whatever its status (see
[SECURITY.md](SECURITY.md#failed-login-throttle)).

### Throttling

Every password check is throttled (details in
[SECURITY.md](SECURITY.md#failed-login-throttle)). Within a 15-minute window
fixed from the first failure, a username is refused after 10 failures from one
client address, and an address after 50 failures for any usernames. A refused
check never tests the password. Every `/api/` endpoint that checks a password
then answers

```
HTTP/1.1 429 Too Many Requests
Content-Type: application/json

{"error": "Too many failed logins"}
```

and `/scripts/` redirects to `/login/` with "Too many failed logins. Try again
later". Back off for the rest of the window. Counters are per gunicorn process
and reset when the container restarts.

### CSRF

All `/api/` views and `/scripts/` are exempt from Django's CSRF middleware so
that scripts can call them with credentials alone. The one state-changing API
endpoint, `POST api/networks/<engine>/<id>/delete/`, adds its own checks
([SECURITY.md](SECURITY.md#sessions-and-state-changing-requests)):

- A request whose `Sec-Fetch-Site` header is `cross-site` or `same-site` is
  refused with 403 `{"error": "Cross-site requests are refused"}`. Scripts do
  not send that header.
- A request that carries a **logged-in session cookie** must also carry a
  valid CSRF token (the `csrftoken` cookie echoed in an `X-CSRFToken` header,
  or a `csrfmiddlewaretoken` form field), or it is refused with 403
  `{"error": "Browser sessions must send a CSRF token"}`, even when the body
  also holds valid credentials.

This matters to scripts: `/scripts/` logs the caller in and sets a
`sessionid` cookie. A `requests.Session` that called `/scripts/` and then
calls the delete endpoint must send the CSRF token, or use a fresh session (or
plain `requests.post`) with credentials only.

The read endpoints and `POST api/active/` change nothing and do not check CSRF.

## Endpoint summary

| Method | Path | Auth | Returns |
|---|---|---|---|
| GET, POST | `/api/config/` | view | Enabled engines and books |
| GET, POST | `/api/config/<engine>/` | view | One engine's configuration |
| GET, POST | `/api/buildinfo/` | view | Every enabled engine's configuration and default network |
| GET, POST | `/api/networks/<engine>/` | view | An engine's networks |
| GET, POST | `/api/networks/<engine>/<sha-or-name>/` | user | The network file |
| POST | `/api/networks/<engine>/<name-or-sha>/delete/` | Approver | Deletes a network |
| GET, POST | `/api/workloads/?status=&engine=&since_id=&limit=` | view | Compact workload rows, with what each is waiting for |
| GET, POST | `/api/live/workloads/?author=&token=` | view | The unfinished listing rows as displayed, or "unchanged" |
| GET, POST | `/api/live/workload/<id>/?token=` | view | One workload's stat block and diagnosis as displayed, or "unchanged" |
| GET, POST | `/api/workload/<id>/results/` | view | Per-machine results |
| GET, POST | `/api/workload/<id>/info/` | view | The workload's fields |
| GET, POST | `/api/workload/<id>/summary/` | view | Results grouped by user, CPU, ISA |
| GET, POST | `/api/workload/<id>/insights/` | view | Progress, ETA, strength, history, diagnosis, results analysis |
| GET, POST | `/api/workload/<id>/history.csv` | view | The insights history as CSV |
| GET, POST | `/api/workload/<id>/games/` | view | Per-game statistics from the PGN archive |
| GET, POST | `/api/spsa/<id>/<inputs\|outputs\|digest\|perturbation>/` | view | SPSA tune parameters |
| GET, POST | `/api/pgns/<id>/` | view | The workload's PGN archive |
| GET, POST | `/api/insights/server/` | view | Fleet and workload counters |
| GET, POST | `/api/progress/?engine=&window=` | view | Engine lineage and activity over a time window |
| GET, POST | `/api/digest/?since=&from=` | view | What finished, ran, moved the trunk and failed in a time window |
| GET, POST | `/api/errors/?workload=&kind=&unresolved=&limit=` | view | Worker errors grouped by workload and summary |
| GET, POST | `/api/errors/<event id>/log/` | view | The log uploaded with one worker error |
| GET, POST | `/api/jump/?q=` | view | Quick-jump suggestions |
| GET, POST | `/api/storage/` | manager | Disk usage of the data directory |
| POST | `/api/active/` | user | Workloads a described machine could be assigned |
| POST | `/scripts/` | user / Approver | Upload a network or create a test (HTML) |
| GET, HEAD | `/health/` | none | Database readiness |

"view" means an enabled user while `require_login_to_view` is `true` (the
default here), otherwise public. "GET" works only with a browser session,
since credentials must be POSTed.

## Configuration

### `GET|POST /api/config/`

Enabled engines (sorted by name) and enabled books, keyed by name.

```json
{
    "engines": [
        "Avalanche"
    ],
    "books": {
        "2moves_v1.epd": {
            "sha": "7bec98239836f219dc41944a768c0506abed950aaec48da69a0782643e90f237",
            "source": "https://raw.githubusercontent.com/AndyGrant/openbench-books/master/2moves_v1.epd.zip"
        },
        "UHO_Lichess_4852_v1.epd": {
            "sha": "7a7f6470615a69c6cf23d565417701d38732876f480af90d67b42abade35644a",
            "source": "https://raw.githubusercontent.com/AndyGrant/openbench-books/master/UHO_Lichess_4852_v1.epd.zip"
        }
    }
}
```

(`books` trimmed from 18 entries.)

### `GET|POST /api/config/<engine>/`

One engine's configuration, in the shape of the `Engines/<name>.json` files it
replaced. Unlike `/api/config/`, it also answers for disabled engines.
`build.compilers`, `build.cpuflags` and `build.systems` are lists; the
`*_presets` objects hold whatever presets the engine defines.

```json
{
    "private": false,
    "nps": 1500000,
    "source": "https://github.com/SnowballSH/Avalanche",
    "build": {
        "path": "",
        "compilers": [
            "zig>=0.16.0"
        ],
        "cpuflags": [],
        "systems": [
            "Linux",
            "Darwin"
        ]
    },
    "test_presets": {
        "default": {}
    },
    "tune_presets": {
        "default": {}
    },
    "datagen_presets": {
        "default": {}
    }
}
```

| Error | Status | Body |
|---|---|---|
| Unknown engine | 404 | `{"error": "Engine not found. Check /api/config/ for a full list"}` |

### `GET|POST /api/buildinfo/`

Every enabled engine's configuration (the object above), keyed by engine name.
An engine that has a default network also gets a `network` object.

```json
{
    "Avalanche": {
        "private": false,
        "nps": 1500000,
        "source": "https://github.com/SnowballSH/Avalanche",
        "build": { "path": "", "compilers": ["zig>=0.16.0"], "cpuflags": [], "systems": ["Linux", "Darwin"] },
        "test_presets": { "default": {} },
        "tune_presets": { "default": {} },
        "datagen_presets": { "default": {} },
        "network": {
            "sha": "AE0B26BB",
            "name": "demo-net-1",
            "author": "admin",
            "created": "2026-09-30 06:48:01.268908+00:00"
        }
    }
}
```

(Reformatted more compactly than the server sends it.)

## Networks

A network is identified per engine by its `name` or its `sha256`, which is
the first eight hex digits of the file's SHA-256, upper-case (for example
`AE0B26BB`). Where a path takes an `<identifier>`, it is matched against
`sha256` first and then `name`, so a network named like another network's SHA
is reached by its SHA. The website's network pages resolve identifiers the
same way. `created` is `str()` of a UTC datetime,
`YYYY-MM-DD HH:MM:SS.ffffff+00:00`, which `datetime.fromisoformat` parses.

### `GET|POST /api/networks/<engine>/`

```json
{
    "default": {
        "default": true,
        "was_default": true,
        "sha256": "AE0B26BB",
        "name": "demo-net-1",
        "engine": "Avalanche",
        "author": "admin",
        "created": "2026-09-30 06:48:01.268908+00:00"
    },
    "networks": [
        {
            "default": false,
            "was_default": false,
            "sha256": "3E922C54",
            "name": "demo-net-2",
            "engine": "Avalanche",
            "author": "admin",
            "created": "2026-09-30 06:48:01.422618+00:00"
        },
        {
            "default": true,
            "was_default": true,
            "sha256": "AE0B26BB",
            "name": "demo-net-1",
            "engine": "Avalanche",
            "author": "admin",
            "created": "2026-09-30 06:48:01.268908+00:00"
        }
    ]
}
```

`default` is `null` when the engine has no default network. `networks` lists
every network of the engine, the default included.

| Error | Status | Body |
|---|---|---|
| Unknown engine | 404 | `{"error": "Engine not found. Check /api/config/ for a full list"}` |

### `GET|POST /api/networks/<engine>/<identifier>/`

Downloads the network file. Always needs an enabled user, whatever
`require_login_to_view` says; any enabled user may download, not only
Approvers.

```
HTTP/1.1 200 OK
Content-Type: application/octet-stream
Content-Length: 4096
Expires: Wed, 07 Oct 2026 07:16:16 GMT
Cache-Control: max-age=604800, private
Content-Disposition: attachment; filename=66174A29

<file bytes>
```

The example is a download by SHA. The filename is always the network's
`sha256`, not its name.

Caching depends on how the URL names the network. A SHA always names the
same bytes, so a download by SHA may be kept by the browser for a week (shared
caches may not, since it needs a login). A name can be moved to other bytes by
renaming or re-uploading, so a download by name is sent with
`Cache-Control: max-age=0, no-cache, no-store, must-revalidate, private` and
is never cached. Scripts that download repeatedly should use the SHA from
`/api/networks/<engine>/`. When the server sets
`use_x_accel_redirect`, the body is served by the reverse proxy and the
response carries `X-Accel-Redirect` instead; the client sees the same result.

Errors come back as JSON, so check the status before writing the body to
disk:

| Error | Status | Body |
|---|---|---|
| Not logged in, bad credentials, account not enabled | 401 | `{"error": "API requires authentication for this endpoint"}` |
| No network with that SHA or name, engine configured | 404 | `{"error": "Network nope for Engine Avalanche not found"}` |
| No such network, engine not configured | 404 | `{"error": "Engine not found. Check /api/config/ for a full list"}` |
| Throttled | 429 | `{"error": "Too many failed logins"}` |

### `POST /api/networks/<engine>/<identifier>/delete/`

Deletes a network. Only for enabled Approvers, as on the website.

```json
{
    "success": "Deleted demo-net-2 for Avalanche"
}
```

Side effects: the `Network` row is deleted, and the file in `Media/` is
removed once no network of any engine still uses that sha. Workloads that name
the network can no longer fetch it.

| Error | Status | Body |
|---|---|---|
| Not `POST` | 405 | `{"error": "POST required"}` |
| `Sec-Fetch-Site: cross-site` or `same-site` | 403 | `{"error": "Cross-site requests are refused"}` |
| Logged-in session without a valid CSRF token | 403 | `{"error": "Browser sessions must send a CSRF token"}` |
| Not logged in, bad credentials, account not enabled | 401 | `{"error": "API requires authentication for this endpoint"}` |
| Enabled, but not an Approver | 403 | `{"error": "Only Approvers may delete Networks"}` |
| No such network (or engine) | 404 | `{"error": "Network nope for Engine Avalanche not found"}` |
| Default or previous default network | 409 | `{"error": "You may not delete Default, or previous Default networks"}` |
| Throttled | 429 | `{"error": "Too many failed logins"}` |

`Scripts/delete_networks.py` uses this endpoint, addressing each network by
its SHA so a name that matches another network's SHA cannot misdirect it.

## Workloads

`<id>` is the workload number shown in its URL (`/test/<id>/`, `/tune/<id>/`,
`/datagen/<id>/`). Deleted workloads are still served.

Errors shared by every workload endpoint:

| Error | Status | Body |
|---|---|---|
| Authentication failed | 401 | `{"error": "API requires authentication for this server"}` |
| No workload with that id | 404 | `{"error": "Requested Workload Id does not exist"}` |
| Throttled | 429 | `{"error": "Too many failed logins"}` |

### `GET|POST /api/workloads/`

Lists workloads as compact rows, so a script need not walk ids until one
answers 404. Each row says what the workload is waiting for.

| Parameter | Values | Default |
|---|---|---|
| `status` | `active` (approved, unfinished), `pending` (awaiting approval), `finished` (passed, failed, completed or stopped; not deleted), `all` (everything, deleted included) | `all` |
| `engine` | A dev engine name, matched exactly | every engine |
| `since_id` | Only workloads with a larger id, oldest first | newest first |
| `limit` | 1 to 200 | 50 |

Parameters are read from the query string, or from the POST body beside the
credentials. Without `since_id` the newest workloads come first. With it the
rows ascend from that id, and `next_since_id` is the value to pass next; it is
`null` once a page comes back short, and whenever `since_id` was not given.
Start a full walk with `since_id=0`.

```bash
curl -s https://openbench.example.org/api/workloads/?status=active \
    --data-urlencode username="$OPENBENCH_USERNAME" \
    --data-urlencode password="$OPENBENCH_PASSWORD"
```

```json
{
    "workloads": [
        {
            "id": 38,
            "mode": "SPRT",
            "status": "active",
            "engine": "Avalanche",
            "dev": { "name": "2ac6a70d4bb394fe1ab3db3e893a99f0f69ec381", "sha": "2ac6a70d4bb394fe1ab3db3e893a99f0f69ec381" },
            "base": { "name": "60637f88df729611fb53231be2aed4ac3d028e6d", "sha": "60637f88df729611fb53231be2aed4ac3d028e6d" },
            "time_control": "8.0+0.08",
            "games": 0,
            "llr": 0.0,
            "llr_lower": -2.94,
            "llr_upper": 2.94,
            "elo": null,
            "created_at": "2026-10-01T05:08:54.377769+00:00",
            "updated_at": "2026-10-01T05:08:54.379537+00:00",
            "info": "Quiet-move history bonus scaling, STC",
            "diagnosis": {
                "state": "outranked",
                "headline": "Outranked: workers take the highest priority first, and 3 workloads at priority 1 are ahead of this one at priority 0."
            }
        }
    ],
    "count": 1,
    "next_since_id": null
}
```

- `mode` is `SPRT`, `GAMES`, `SPSA` or `DATAGEN`; `status` is `pending`,
  `active`, `passed`, `failed`, `completed`, `stopped` or `deleted`, as in the
  insights.
- `engine` and `time_control` are the dev side's.
- `llr`, `llr_lower` and `llr_upper` are `null` unless `mode` is `SPRT`.
- `elo` is `{ "lower", "value", "upper" }` with a 95% interval, `null` for a
  tune and before two games (or pairs) are in.
- `info` is the first line of the workload's info.
- `diagnosis.state` and `diagnosis.headline` are the verdict of
  [INSIGHTS.md](INSIGHTS.md#workload-diagnosis); the evidence behind it is in
  `/api/workload/<id>/insights/`. A finished workload reads `finished`, one
  that a worker's bench mismatch stopped `stopped_by_error` with the error in
  its headline, a pending one `awaiting_approval`.

| Error | Status | Body |
|---|---|---|
| Unknown `status` | 400 | `{"error": "status must be one of active, pending, finished, all"}` |
| `limit` not a whole number from 1 to 200 | 400 | `{"error": "limit must be a whole number from 1 to 200"}` |
| `since_id` not a whole number | 400 | `{"error": "since_id must be a whole number"}` |

The response costs a fixed number of queries whatever the `limit`: the rows,
plus the six of the diagnosis when a row is active and one more when a row is
stopped.

### `GET|POST /api/live/workloads/` and `/api/live/workload/<id>/`

The two endpoints `OpenBench/static/live.js` polls so that the index and a
workload page follow a running test without a reload ([UI.md](UI.md#live-updates)).
They answer with **display values**: the same strings, colours and fractions
the templates render, built by the same functions (`shortStatBlock`,
`longStatBlock`, `workload_progress`, `listing_row_timing`, `row_reason`,
`listing_moment`, and the page's own `FrontPage` queries), so the page and
the poll cannot drift apart. A script that wants numbers should read
`/api/workloads/` or the insights instead.

Every response carries a `token`. Send it back as `token=` and, while nothing
changed, the answer is only

```json
{ "token": "253ab8c2e80da3a0", "changed": false }
```

for four queries: the session, the user, the Profile, and one read of the
token's inputs. The token is a digest, not a timestamp; treat it as opaque.

- For the listing it covers the unfinished, undeleted workloads in scope:
  how many there are, how many are approved, their newest `updated` and their
  summed games. A result, an approval, a new workload, a finish or a stop
  each change it. Every write to a `Test` goes through `save()`, which moves
  `updated`.
- For one workload it covers its `updated`, games and state flags.
- Both also fold in the current minute, so an idle page is re-sent at most
  once a minute. That keeps the time-derived text ("2h left", "no workers
  for 12m") from freezing while no games arrive.

It is a query parameter rather than `ETag`/`If-None-Match` so that it means
the same thing to a POSTing script and survives any proxy that rewrites
validators when it compresses a response.

| Parameter | Endpoint | Values |
|---|---|---|
| `token` | both | The `token` of the previous answer, at most 64 characters |
| `author` | listing | Only this author's workloads, as on `/user/<name>/`; at most 150 characters |

The listing holds the pending workloads, then the active ones in index
order:

```json
{
    "token": "253ab8c2e80da3a0",
    "changed": true,
    "machine_status": ": 3 Machines / 88 Threads / 137.6 MNPS ",
    "rows": [
        {
            "id": 2,
            "result": {
                "status": "active",
                "games": 5240,
                "colour": "",
                "outcome": "Running",
                "statblock": [
                    "LLR: 0.50 (-2.94, 2.94) [0.00, 3.00]",
                    "Games: 5240 W: 946 L: 898 D: 3396",
                    "Ptnml(0-2): 134, 627, 1058, 662, 139"
                ]
            },
            "progress": { "kind": "llr", "fraction": 0.5853, "label": "LLR 0.50 between bounds -2.94 and 2.94" },
            "timing": {
                "kind": "left",
                "text": "19d 17h left",
                "estimate": true,
                "note": "Assumes the test keeps producing results like it has so far; an order of magnitude, not a promise.",
                "rate": "143 games/h",
                "rate_window": "last 1h"
            },
            "reason": null,
            "moment": { "verb": "started", "at": "2026-09-30T05:10:11.120000+00:00", "ago": "1d ago" }
        }
    ]
}
```

- `result.status` is the insights status; a listing row is only ever
  `pending` or `active`. `colour` is `green`, `blue`, `yellow`, `red` or
  empty, and `outcome` the words that stand in for it
  (`Blocks/result_text.html`).
- `progress` is the bar under an active row (`kind` is `llr` or `games`),
  `null` for a pending row and for a tune.
- `timing`, `reason` (`severity`, `headline`, `brief`) and `moment` are the
  lines below the stat block, each `null` when the row shows none.
- `machine_status` is the text after "Active" in the table heading.

One workload:

```json
{
    "token": "3a066758db194dc3",
    "changed": true,
    "workload": {
        "id": 1,
        "result": { "status": "passed", "games": 10420, "colour": "green", "outcome": "Passed", "statblock": ["…"] },
        "diagnosis": null
    }
}
```

`result.statblock` is the page's long block (the short one for a tune).
`diagnosis` is the banner of [INSIGHTS.md](INSIGHTS.md#workload-diagnosis)
with `state`, `severity`, `headline`, `brief`, `evidence` and `urgent`, and `null` once
the workload has finished.

| Error | Status | Body |
|---|---|---|
| `token` or `author` too long | 400 | `{"error": "token must be at most 64 characters"}` |
| Authentication failed | 401 | `{"error": "API requires authentication for this server"}` |
| No workload with that id | 404 | `{"error": "Requested Workload Id does not exist"}` |

A changed listing costs up to 13 queries whatever the number of rows: the
four above, the pending and active rows, the machine status, and the six of
the diagnosis, which are spent only when an active row has no recent rate
(7 queries while every active row is producing games). A changed workload
costs up to 10.

### `GET|POST /api/workload/<id>/<query>/`

`<query>` is one of `results`, `info`, `summary`, `insights`. Any other value
answers 404 `{"error": "Valid /query/ endpoints are: [ results, info, summary, insights ]"}`.

#### `info`

Every field of the `Test` row, with `dev` and `base` as nested engine objects,
`tri` as `[losses, draws, wins]` and `penta` as `[LL, LD, DD, DW, WW]`.

```json
{
    "info": {
        "id": 3,
        "author": "admin",
        "upload_pgns": "FALSE",
        "info": "Seeded demonstration workload",
        "book_name": "UHO_Lichess_4852_v1.epd",
        "book_index": 1,
        "dev_repo": "https://github.com/SnowballSH/Avalanche",
        "dev_engine": "Avalanche",
        "dev_options": "Threads=1 Hash=16",
        "dev_network": "BCF481FD",
        "dev_netname": "nezha",
        "dev_time_control": "8.0+0.08",
        "base_repo": "https://github.com/SnowballSH/Avalanche",
        "base_engine": "Avalanche",
        "base_options": "Threads=1 Hash=16",
        "base_network": "BCF481FD",
        "base_netname": "nezha",
        "base_time_control": "8.0+0.08",
        "workload_size": 32,
        "priority": 0,
        "throughput": 1000,
        "scale_method": "BASE",
        "scale_nps": 0,
        "syzygy_wdl": "OPTIONAL",
        "syzygy_adj": "OPTIONAL",
        "win_adj": "movecount=3 score=400",
        "draw_adj": "movenumber=40 movecount=8 score=10",
        "test_mode": "SPRT",
        "elolower": 0.0,
        "eloupper": 5.0,
        "alpha": 0.05,
        "beta": 0.05,
        "lowerllr": -2.94,
        "currentllr": 2.945188892528937,
        "upperllr": 2.94,
        "max_games": 0,
        "genfens_args": "",
        "play_reverses": false,
        "games": 16800,
        "use_tri": false,
        "use_penta": true,
        "passed": true,
        "failed": false,
        "finished": true,
        "deleted": false,
        "approved": true,
        "error": false,
        "dev": {
            "id": 5,
            "name": "nezha-v2",
            "source": "https://api.github.com/repos/SnowballSH/Avalanche/zipball/07c49c6391d600f8d71c68c0d16c70a8d139a0fb",
            "sha": "07c49c6391d600f8d71c68c0d16c70a8d139a0fb",
            "bench": 3053036
        },
        "base": {
            "id": 6,
            "name": "master",
            "source": "https://api.github.com/repos/SnowballSH/Avalanche/zipball/02e1ed217916472d3565e5f3cbdecf01ccc5a6c5",
            "sha": "02e1ed217916472d3565e5f3cbdecf01ccc5a6c5",
            "bench": 2853115
        },
        "tri": [2835, 10911, 3054],
        "penta": [417, 2001, 3367, 2176, 439],
        "creation": "2026-09-27 20:51:03.498705+00:00",
        "updated": "2026-09-29 05:38:53.790194+00:00"
    }
}
```

`test_mode` is `SPRT`, `GAMES`, `SPSA` or `DATAGEN`. A workload is running
while `approved` is true and `finished` and `deleted` are false.

#### `results`

One entry per Machine registration that played games for the workload, or is
on it now; a Client that restarted mid-workload appears once per start (the
insights payload below pools them per physical machine).
`active` is true when the machine reported within the last minute and is
still assigned this workload.

```json
{
    "results": [
        {
            "machine__id": 1,
            "machine__user__username": "lab-worker",
            "games": 2682,
            "LL": 60,
            "LD": 327,
            "DD": 542,
            "DW": 337,
            "WW": 75,
            "timeloss": 0,
            "crashes": 0,
            "active": false
        }
    ]
}
```

(Trimmed to the first entry.)

#### `summary`

The results grouped three ways, as in the tables on the workload page: by
`user`, `cpu_name` and `isa_name`. Values are display strings where the page
shows formatted text (`penta`, `elo`, `percent`).

```json
{
    "summary": {
        "user": [
            {
                "key": "home-worker",
                "penta": "(96, 378, 638, 425, 78)",
                "elo": "1.18 ± 8.17",
                "pairs": 1615,
                "percent": "52.10",
                "dev_nps": 2100731,
                "dev_nps_scaled": 2100731,
                "base_nps": 2058717,
                "base_nps_scaled": 2058717
            }
        ],
        "cpu_name": [
            {
                "key": "Apple M4",
                "penta": "(65, 226, 383, 276, 47)",
                "elo": "2.44 ± 10.54",
                "pairs": 997,
                "percent": "32.16",
                "dev_nps": 2400000,
                "dev_nps_scaled": 2400000,
                "base_nps": 2352000,
                "base_nps_scaled": 2352000
            }
        ],
        "isa_name": [ ... ]
    }
}
```

(Each list trimmed to one entry.)

#### `insights`

Progress, throughput, ETA, strength, history and contributions, all as JSON
numbers; `diagnosis`: what the workload is waiting for, as a state, a
headline and the evidence behind it
([INSIGHTS.md](INSIGHTS.md#workload-diagnosis)); and `results`: the results
analysis (verdict, outcome breakdown, SPRT outlook, search speed,
consistency; [INSIGHTS.md](INSIGHTS.md#results)). The full schema, including every `eta.kind` and `reason`, is in
[INSIGHTS.md](INSIGHTS.md#api).

```json
{
    "insights": {
        "generated_at": "2026-09-30T06:49:07.170616+00:00",
        "workload": {
            "id": 3,
            "mode": "SPRT",
            "status": "passed",
            "use_penta": true,
            "created_at": "2026-09-27T20:22:49.276633+00:00",
            "updated_at": "2026-09-29T05:10:39.568122+00:00"
        },
        "progress": {
            "games": 6200,
            "pairs": 3100,
            "trinomial": [1074, 4031, 1095],
            "pentanomial": [166, 742, 1248, 793, 151],
            "llr": -0.19240262646888878,
            "llr_lower": -2.94,
            "llr_upper": 2.94,
            "target_games": null,
            "fraction": null
        },
        "timing": {
            "started_at": "2026-09-27T20:38:11.882124+00:00",
            "ended_at": "2026-09-29T05:10:39.568122+00:00",
            "elapsed_seconds": 117147.685998,
            "overall": { "games_per_hour": 188.99220937559096, "window_seconds": 117147.685998 },
            "recent": { "games_per_hour": 210.94178207756522, "window_seconds": 3600.0 }
        },
        "eta": {
            "kind": "finished",
            "remaining_games": 0,
            "remaining_seconds": 0.0,
            "completes_at": null,
            "reason": null
        },
        "strength": {
            "elo": { "lower": -4.640502309128021, "value": 1.1768024512365398, "upper": 6.994767170350524 },
            "normalized_elo": { "lower": -6.897977877407048, "value": 1.7502384498765362, "upper": 10.39845477716012 },
            "los": 0.6541910206111968,
            "draw_ratio": 0.6501612903225806,
            "penta_fractions": [0.053548387096774196, 0.23935483870967741, 0.40258064516129033, 0.2558064516129032, 0.048709677419354835]
        },
        "history": {
            "synthetic": false,
            "points": [
                {
                    "timestamp": "2026-09-29T05:10:39.568122+00:00",
                    "games": 6200,
                    "llr": -0.19240262646888878,
                    "elo": 1.1768024512365398,
                    "elo_lower": -4.640502309128021,
                    "elo_upper": 6.994767170350524
                }
            ]
        },
        "contributions": {
            "machines": [
                {
                    "machine_id": 4,
                    "machine_name": "demo-4",
                    "machine_label": "demo-4",
                    "pool": "demo-4",
                    "owner": "home-worker",
                    "cpu_name": "Apple M4",
                    "registrations": [{ "machine_id": 4, "games": 1994, "pairs": 997 }],
                    "stats": {
                        "games": 1994,
                        "pairs": 997,
                        "share": 0.32161290322580643,
                        "pairs_per_hour": 30.63824922722995,
                        "elo": { "lower": -8.09971111109712, "value": 2.4394072845802235, "upper": 12.983018153406162 }
                    }
                }
            ],
            "cpus": [
                {
                    "cpu_name": "Apple M4",
                    "machines": 1,
                    "stats": {
                        "games": 1994,
                        "pairs": 997,
                        "share": 0.32161290322580643,
                        "pairs_per_hour": 30.63824922722995,
                        "elo": { "lower": -8.09971111109712, "value": 2.4394072845802235, "upper": 12.983018153406162 }
                    }
                }
            ]
        },
        "diagnosis": {
            "state": "finished",
            "severity": "ok",
            "headline": "Not applicable: this workload has finished, so no worker will take it.",
            "brief": "finished",
            "evidence": []
        }
    }
}
```

(`history.points` trimmed from 150 entries to the last, `machines` from 5 and
`cpus` from 4 to the first; objects reformatted compactly. The `results`
object is left out here; its schema is in [INSIGHTS.md](INSIGHTS.md#api).)

Each `machines` entry is one physical machine: the Machine registrations of a
host are pooled, `machine_id` is the newest of them that played this workload,
and `registrations` lists each one's share. See
[INSIGHTS.md](INSIGHTS.md#hosts).

### `GET|POST /api/workload/<id>/history.csv`

The `insights` history (`history.points`) as a CSV download, one row per
point in time order, with the same authentication and the same failures as
`api/workload/<id>/<query>/`: 401 or 404 with a JSON `{"error": "..."}` body.
The id is at most 18 digits; a longer one does not match the route and gets
the site's HTML 404 page, as on the other `<id>` routes. Rows end in CRLF, as
RFC 4180 specifies.

```
Content-Type: text/csv; charset=utf-8
Content-Disposition: attachment; filename="workload-<id>-history.csv"
```

```csv
timestamp,games,llr,elo,elo_lower,elo_upper
2026-09-28T12:02:11.482913+00:00,64,0.089,43.7,-21.2,111.7
2026-09-28T12:03:14.020117+00:00,128,,,,
```

The columns and their empty cells are described in
[INSIGHTS.md](INSIGHTS.md#history-csv).

### `GET|POST /api/spsa/<id>/<query>/`

Parameters of an SPSA tune. `inputs`, `outputs` and `digest` are
`text/plain`; `perturbation` is JSON. The examples come from the seeded
`search-tune`.

| Error | Status | Body |
|---|---|---|
| The workload is not an SPSA tune | 404 | `{"error": "Requested Workload is not an SPSA tune"}` |
| Any other `<query>` | 404 | `{"error": "Valid /query/ endpoints are: [ inputs, outputs, digest, perturbation ]"}` |

#### `inputs`

The parameter lines as submitted, one per parameter, in the tune form's input
format `name, type, start, min, max, c_end, r_end`. Numbers are printed as
floats, even for `int` parameters.

```
LmrBase, float, 0.75, 0.25, 1.5, 0.08, 0.002
LmrDivisor, float, 2.25, 1.5, 3.5, 0.15, 0.002
RfpMargin, int, 75.0, 30.0, 150.0, 8.0, 0.002
NmpBaseReduction, int, 3.0, 1.0, 6.0, 0.5, 0.002
AspirationWindow, int, 12.0, 5.0, 40.0, 3.0, 0.002
HistoryDivisor, int, 8192.0, 2048.0, 16384.0, 600.0, 0.002
```

#### `outputs`

The current values, `int` parameters rounded to integers.

```
LmrBase, 0.8405858306125237
LmrDivisor, 2.090755051326147
RfpMargin, 66
NmpBaseReduction, 4
AspirationWindow, 14
HistoryDivisor, 9046
```

#### `digest`

CSV with a header row: the current value, the bounds, and the `C` and `R` a
worker would be assigned now, beside their end values.

```
Name,Curr,Start,Min,Max,C,C_end,R,R_end
LmrBase,0.8406,0.7500,0.2500,1.5000,0.0887,0.0800,0.0027,0.0020
LmrDivisor,2.0908,2.2500,1.5000,3.5000,0.1663,0.1500,0.0027,0.0020
RfpMargin,66.2473,75,30,150,8.8686,8.0000,0.0027,0.0020
NmpBaseReduction,3.7927,3,1,6,0.5543,0.5000,0.0027,0.0020
AspirationWindow,14.4807,12,5,40,3.3257,3.0000,0.0027,0.0020
HistoryDivisor,9046.3813,8192,2048,16384,665.1445,600.0000,0.0027,0.0020
```

#### `perturbation`

A sample of the assignment a worker running four concurrent games would
receive now: for each parameter, the `dev` and `base` values per runner, the
random `flip` signs, and the current `c` and `r`. Each call draws new random
signs. It is computed only; nothing is stored or assigned.

```json
{
    "perturbation": {
        "LmrBase": {
            "index": 0,
            "dev": [0.9292717604879932, 0.9292717604879932, 0.9292717604879932, 0.9292717604879932],
            "base": [0.7518999007370541, 0.7518999007370541, 0.7518999007370541, 0.7518999007370541],
            "flip": [1, 1, 1, 1],
            "c": 0.08868592987546958,
            "r": 0.0027492324321343807
        },
        "LmrDivisor": {
            "index": 1,
            "dev": [2.2570411698426525, 2.2570411698426525, 2.2570411698426525, 2.2570411698426525],
            "base": [1.9244689328096416, 1.9244689328096416, 1.9244689328096416, 1.9244689328096416],
            "flip": [1, 1, 1, 1],
            "c": 0.16628611851650546,
            "r": 0.0027492324321343803
        }
    }
}
```

(Trimmed to the first two of six parameters; arrays reformatted onto one
line. This tune uses the `SINGLE` distribution, so all four runners share one
perturbation.)

### `GET|POST /api/workload/<id>/games/`

Statistics over the games in the workload's PGN archive: results by colour,
pair outcomes, how games ended, game length, openings and evaluations.

```json
{ "games": { "status": "disabled", "upload_pgns": "FALSE", "active": true, "report": null } }
```

`status` is `disabled` for a workload created without PGN uploads, `empty`
until its first batch is archived, and `ready` with a `report` after that.
`report.limits.complete` is `false` while the archive is still being read;
ask again to advance it. The full schema, every definition and the limits are
in [INSIGHTS.md](INSIGHTS.md#getpost-apiworkloadidgames).

| Error | Status | Body |
|---|---|---|
| Authentication failed | 401 | `{"error": "API requires authentication for this server"}` |
| No workload with that id | 404 | `{"error": "Requested Workload Id does not exist"}` |

The path is routed before `api/workload/<id>/<query>/`, so `games` is not in
the list of endpoints that route's 404 names.

### `GET|POST /api/pgns/<id>/`

Downloads `Media/PGNs/<id>.pgn.tar`, the archive the PGN watcher builds for a
workload created with `upload_pgns` enabled. It holds one bzip2 file per
uploaded batch.

```
HTTP/1.1 200 OK
Content-Type: application/octet-stream
Content-Length: 3072
Expires: Wed, 30 Sep 2026 07:16:27 GMT
Cache-Control: max-age=0, no-cache, no-store, must-revalidate, private
Content-Disposition: attachment; filename=3.pgn.tar

<tar bytes>
```

The archive is never cached: `Expires` is the time of the response.

The checks run in this order:

| Error | Status | Body |
|---|---|---|
| Authentication failed | 401 | `{"error": "API requires authentication for this server"}` |
| No workload with that id | 404 | `{"error": "Requested Workload Id does not exist"}` |
| No archive on disk | 404 | `{"error": "Unable to find PGN for Workload #4"}` |
| Workload not finished | 409 | `{"error": "PGNs cannot be downloaded while the Workload is active"}` |
| A machine that reported in the last 2 minutes is still on it | 409 | `{"error": "Some machines are still on this Workload. Try again shortly"}` |
| Batches not yet archived | 409 | `{"error": "Still processing individual PGNs into the archive. Try again shortly"}` |
| Throttled | 429 | `{"error": "Too many failed logins"}` |

The last two 409s clear on their own; retry after a short wait.
`Scripts/archive2pgns.py` and `Scripts/archive2nps.py` read these archives.

## Server

### `GET|POST /api/insights/server/`

Fleet and workload counters for the whole server, as on the index page.
Schema in [INSIGHTS.md](INSIGHTS.md#getpost-apiinsightsserver).

```json
{
    "server": {
        "generated_at": "2026-09-30T06:49:30.255058+00:00",
        "fleet": {
            "machines": 0,
            "threads": 0,
            "mnps": 0
        },
        "workloads": {
            "pending": 1,
            "active": 3
        },
        "games_last_24h": 14246,
        "finished_last_7d": {
            "window_days": 7,
            "total": 3,
            "passed": 2,
            "failed": 1,
            "stopped": 0,
            "sprt_passed": 1,
            "sprt_failed": 1,
            "sprt_pass_rate": 0.5
        },
        "top_contributors": [
            {
                "username": "lab-worker",
                "games": 16770
            },
            {
                "username": "home-worker",
                "games": 11430
            }
        ]
    }
}
```

| Error | Status | Body |
|---|---|---|
| Authentication failed | 401 | `{"error": "API requires authentication for this server"}` |
| Throttled | 429 | `{"error": "Too many failed logins"}` |

### `GET|POST /api/progress/?engine=&window=`

`progress.release` is the engine's progress against its latest release:
`anchor` (the release's `tag`, `sha`, `published_at`, the `default_branch`,
whether it is `pinned` by the operator, its recorded `bench` and `network`,
and `fetched_at` / `attempted_at` / `error` of the GitHub lookup; `null` until
one was attempted), `series` (per
time-control class, the `points` measured directly against the release from
default-branch commits, oldest first, and `latest`, the newest finished one:
the headline), `branches` (the same points for commits not known to be on
the default branch, never part of the headline) and `sprt` (SPRT runs against
the release, which stop early and are never pooled with the fixed-games runs
the series are made of). Each point has `base`, `dev`,
`committed_at`, `measured_at`, `first_run`, `newer_bases` and a pooled
`measurement` shaped like a lineage step's. `release` is `null` when no single
engine is charted, and ignores `window`. No request to this endpoint ever
reaches GitHub; the release is refreshed in the background.

It is followed by the engine's commit lineage (trunk steps with one pooled Elo measurement per
time-control class, candidates that branched off, chained estimates per class
and direct checks), its economics (per-step bench, search-speed ratio and
cost; search speed chained along the trunk; games and search core-hours spent
on trunk steps, failed candidates and other candidates; pass rate, median
games to pass and to fail and games per Elo by class; trunk steps per week and
acceptance latencies), weekly SPRT outcomes, games per day and top contributors
over a window. `window` is `30d`, `90d` (default), `1y` or `all`,
ignoring case and surrounding whitespace; `engine` filters by the workloads'
dev engine. Authentication is the same as `api/insights/server/`: a failed
login is 401, an unknown `window` is 400 `{"error": ...}`, and an `engine`
with no Engine configuration is 404 `{"error": ...}`. Reports are cached
for 60 seconds per window and configured engine. `progress.lineage` is `null`
when there is nothing to chain, or when no engine is chosen and several have
steps (`progress.lineage_engines` names them); `progress.economics` is `null`
in the same cases. The earlier `greens`,
`elo_steps` and `summary.elo_gained` fields are gone. The JSON schema, the
model, formulas and caveats are
in [INSIGHTS.md](INSIGHTS.md#engine-progress).

### `GET|POST /api/digest/?since=&from=`

What happened in a time window, the data behind `/digest/`: workloads that
finished, workloads still running with what each is waiting for, trunk steps
measured per time-control class with their chained Elo, fleet games, search
core-hours, hosts and pools, and the worker error groups seen. What each
field means is in [INSIGHTS.md](INSIGHTS.md#digest).

| Parameter | Meaning |
|---|---|
| `since` | `8h`, `24h` (default), `3d` or `7d` |
| `from` | Instead of `since`: an ISO 8601 timestamp with seconds and an offset, such as `2026-10-01T06:00:00Z`. More than 30 days back is clamped to 30 days and reported as `window.clamped` |

Either may be sent in the query string or in the POST body. A `from` window
starts on the whole UTC minute at or before the instant. Reports are cached
for 60 seconds per window.

```json
{
    "digest": {
        "generated_at": "2026-10-01T09:16:18.275479+00:00",
        "window": {
            "preset": "24h",
            "since": "2026-09-30T09:16:18.275479+00:00",
            "until": "2026-10-01T09:16:18.275479+00:00",
            "clamped": false
        },
        "headline": [
            "5 workloads finished in the last 24 hours (1 passed, 2 failed, 2 completed), 8 still running, 1 awaiting approval, 1 unresolved error group.",
            "The fleet played 77,352 games on 5 pools, at least 535.3 core-h of search."
        ],
        "finished": {
            "counts": {"total": 5, "passed": 1, "failed": 2, "completed": 2},
            "workloads": [
                {
                    "workload": {
                        "id": 4,
                        "url": "/test/4/",
                        "title": "aspiration-width",
                        "commits": null,
                        "author": "admin",
                        "engine": "Avalanche",
                        "mode": "SPRT",
                        "time_control": "8.0+0.08",
                        "time_class": "stc"
                    },
                    "status": "failed",
                    "elo": {"lower": -5.67, "value": -2.62, "upper": 0.43},
                    "games": 22300,
                    "started_at": "2026-09-30T17:01:19.780516+00:00",
                    "finished_at": "2026-10-01T01:45:03.782023+00:00",
                    "duration_seconds": 31424.001507,
                    "stop_reason": null
                }
            ],
            "omitted": 0
        },
        "running": {
            "total": 9,
            "pending": 1,
            "started": 2,
            "workloads": [
                {
                    "workload": {"id": 38, "url": "/test/38/", "title": "Widen aspiration windows after a fail high", "commits": "2ac6a70d vs 60637f88", "author": "lab-worker", "engine": "Avalanche", "mode": "SPRT", "time_control": "8.0+0.08", "time_class": "stc"},
                    "status": "active",
                    "started_in_window": true,
                    "started_at": "2026-09-30T19:57:38.616919+00:00",
                    "games": 1800,
                    "elo": {"lower": -8.01, "value": 2.51, "upper": 13.03},
                    "llr": {"value": 0.104, "lower": -2.94, "upper": 2.94},
                    "games_per_hour": 131.34,
                    "eta": {
                        "kind": "sprt_estimate",
                        "remaining_games": 48010,
                        "remaining_seconds": 1315942.53,
                        "completes_at": "2026-10-16T14:48:40.806832+00:00",
                        "reason": null
                    },
                    "diagnosis": {
                        "state": "outranked",
                        "severity": "warning",
                        "headline": "Outranked: workers take the highest priority first, and 2 workloads at priority 1 are ahead of this one at priority 0.",
                        "brief": "outranked by priority 1"
                    }
                }
            ],
            "omitted": 0
        },
        "trunk": [
            {
                "engine": "Avalanche",
                "head": {"sha": "1236f2a0b59f26ca4e41e7f3004b6ecfc1203ff7", "network": "BCF481FD"},
                "trunk_length": 3,
                "classes": [
                    {
                        "time_class": "ltc",
                        "moves": [
                            {
                                "index": 3,
                                "base": {"sha": "60637f88df729611fb53231be2aed4ac3d028e6d", "network": "BCF481FD"},
                                "dev": {"sha": "1236f2a0b59f26ca4e41e7f3004b6ecfc1203ff7", "network": "BCF481FD"},
                                "repo": "https://github.com/SnowballSH/Avalanche",
                                "subject": "Pawn static-eval correction history (corrhist-pawn), indexed by pawn structure and side",
                                "verdict": "running",
                                "provisional": true,
                                "elo": {"lower": -7.42, "value": 1.36, "upper": 10.15},
                                "games": 2800,
                                "measured_at": null,
                                "remeasured": false,
                                "runs": [37]
                            }
                        ],
                        "moves_omitted": 0,
                        "measured": 0,
                        "accepted": 0,
                        "provisional": 1,
                        "remeasured": 0,
                        "net": null
                    }
                ]
            }
        ],
        "fleet": {
            "games": 77352,
            "core_hours": 535.3262368720085,
            "core_hours_estimated": false,
            "games_without_hours": 36744,
            "hosts": 8,
            "pools": [
                {"owner": "lab-worker", "label": "batch-*", "cpu_name": "AMD EPYC 9R14", "hosts": 4}
            ],
            "pools_omitted": 0,
            "bucket_hours": 1,
            "classes": ["stc", "ltc", "smp", "other"],
            "buckets": [
                {"start": "2026-10-01T08:00:00+00:00", "games": [436, 100, 28, 774]},
                {"start": "2026-10-01T09:00:00+00:00", "games": [142, 22, 4, 112]}
            ],
            "peak_games_per_hour": 5546.0,
            "peak_at": "2026-09-30T17:00:00+00:00"
        },
        "errors": {
            "total": 3,
            "new": 2,
            "unresolved": 1,
            "omitted": 0,
            "truncated": false,
            "groups": []
        }
    }
}
```

Numbers are shortened and arrays trimmed here: `workloads`, `moves`, `pools`
and `buckets` hold every row, and `buckets[].games` follows the order of
`fleet.classes`. `errors.groups` holds the group objects of `api/errors/`
below, each with a `new` flag.

| Error | Status | Body |
|---|---|---|
| Authentication failed | 401 | `{"error": "API requires authentication for this server"}` |
| Unknown `since` | 400 | `{"error": "since must be one of 8h, 24h, 3d, 7d"}` |
| Malformed `from` | 400 | `{"error": "from must be an ISO 8601 timestamp with an offset, such as 2026-10-01T06:00:00Z"}` |
| `from` in the future | 400 | `{"error": "from must not be in the future"}` |
| Both sent | 400 | `{"error": "send since or from, not both"}` |

### `GET|POST /api/errors/?workload=&kind=&unresolved=&limit=`

What is failing: the worker errors behind `/errors/`, grouped by workload and
normalised summary, most recently seen first. Kinds, grouping and the status
rules are in [INSIGHTS.md](INSIGHTS.md#worker-errors).

| Parameter | Meaning |
|---|---|
| `workload` | Only this workload id |
| `kind` | `build`, `bench`, `crash`, `timeloss`, `illegal`, `genfens`, `other`, or `game` for crash, time loss and illegal move together. An unknown value is ignored |
| `unresolved` | `1`, `true`, `on` or `yes` leaves out the groups whose status is `resolved` |
| `summary` | Only events whose stored summary is exactly this text. Repeatable, at most 50 values |
| `limit` | Groups returned, 1 to 100; 25 by default |

Each parameter is read from the query string, or from the POST body beside
the credentials; the query string wins.

```json
{
    "as_of": "2026-10-01T06:51:05.120000+00:00",
    "total": 2,
    "truncated": false,
    "groups": [
        {
            "workload": {
                "id": 9,
                "exists": true,
                "url": "/test/9/",
                "title": "Pawn static-eval correction history (corrhist-pawn)",
                "commits": "741d6dc4 vs 874c026d",
                "engine": "Avalanche",
                "time_control": "8.0+0.08",
                "finished": false,
                "deleted": false,
                "games": 0
            },
            "kind": "build",
            "title": "Avalanche build failed",
            "subject": "741d6dc4",
            "count": 14,
            "first_seen": "2026-10-01T06:37:05.120000+00:00",
            "last_seen": "2026-10-01T06:50:05.120000+00:00",
            "status": "happening",
            "reason": "seen in the last 10 minutes",
            "affected": {
                "registrations": 14,
                "hosts": 13,
                "pruned": 1,
                "sampled": false,
                "pools": [
                    {
                        "label": "batch-*",
                        "cpu": "AMD EPYC 9R14",
                        "hosts": 13
                    }
                ]
            },
            "bench": null,
            "latest_event": 99,
            "log_url": "/api/errors/99/log/"
        },
        {
            "workload": {
                "id": 8,
                "exists": false
            },
            "kind": "bench",
            "title": "Wrong Bench",
            "subject": "Avalanche-28C4E45C",
            "count": 1,
            "first_seen": "2026-09-29T02:10:44+00:00",
            "last_seen": "2026-09-29T02:10:44+00:00",
            "status": "resolved",
            "reason": "workload no longer exists",
            "affected": {
                "registrations": 1,
                "hosts": 1,
                "pruned": 0,
                "sampled": false,
                "pools": [
                    {
                        "label": "demo-1",
                        "cpu": "AMD Ryzen 9 7950X",
                        "hosts": 1
                    }
                ]
            },
            "bench": {
                "reported": [
                    2780000
                ],
                "expected": null,
                "difference": null
            },
            "latest_event": 100,
            "log_url": null
        }
    ]
}
```

- `total` counts the groups that match, before `limit`. `truncated` is true
  when more than 500 (workload, summary) pairs matched and only the 500 most
  recently seen were grouped.
- `status` is `happening`, `quiet` or `resolved`, and `reason` says why in
  words.
- `title` and `subject` are the normalised summary: `subject` is the branch
  (first eight digits of a commit) for a build failure, the binary for a
  bench or genfens failure, and empty for a game error.
- `title` and `subject` are cut to 128 characters, ending in `…` when cut; a
  summary with no text is titled `(no summary)`.
- `affected.registrations` counts distinct Machine rows that reported it,
  `hosts` the distinct hosts among those still registered, `pruned` the ones
  whose row is gone. `sampled` is true when the counts were taken over the
  newest 900 reporters of the returned workloads rather than all of them;
  `registrations` of a group that merges several summaries is then an upper
  bound.
- `bench` is null except for a wrong bench: every number reported, the
  workload's expected bench for that binary (null when the binary matches
  neither engine or the workload is gone) and newest reported minus expected.
- `log_url` is the log of the newest event in the group that has one, or
  null: see the next endpoint. `/event/<latest_event>/` is its page.
- A workload row that no longer exists comes back as
  `{"id": <id>, "exists": false}`.

| Error | Status | Body |
|---|---|---|
| Authentication failed | 401 | `{"error": "API requires authentication for this server"}` |
| Throttled | 429 | `{"error": "Too many failed logins"}` |

### `GET|POST /api/errors/<event id>/log/`

The log a worker uploaded with one error event, whole and unmodified, as
`text/plain; charset=utf-8` with
`Content-Disposition: attachment; filename="event<id>.log"`. It is worker
input: a build log or a PGN, of any size the worker sent. Treat it as
untrusted text.

| Error | Status | Body |
|---|---|---|
| Authentication failed | 401 | `{"error": "API requires authentication for this server"}` |
| No such error event, or it has no stored log | 404 | `{"error": "No logs for event exist"}` |
| Throttled | 429 | `{"error": "Too many failed logins"}` |

### `GET|POST /api/jump/?q=`

Suggestions for the header's [quick jump](UI.md#quick-jump): at most eight
places `q` could mean, best first. Direct hits lead (the workload with that
id, a user or engine with exactly that name, a machine), then the newest
workloads whose info, branch names or commit shas match every term of `q`,
and last a link to the full search. `q` is read from the query string and
capped at 100 characters; an empty or longer `q` gives an empty list. Deleted
workloads are never suggested, and a commit-pinned branch name matches by
its start only. Every
`url` is a path on this server. Authentication is the same as
`api/insights/server/`.

```json
{
    "suggestions": [
        {
            "label": "#6 Pawn static-eval correction history (corrhist-pawn), LTC confirmation of #5",
            "detail": "76f2da3c vs 8c308d43 \u00b7 40.0+0.40",
            "url": "/test/6/"
        },
        {
            "label": "Search for \u201ccorrhist\u201d",
            "detail": "Search",
            "url": "/search/?q=corrhist"
        }
    ]
}
```

### `GET|POST /api/storage/`

What fills the data directory. Needs a manager: 401 without a login, 403 for
an enabled user who is not a manager. Sizes are bytes;
the scan is cached for 60 seconds per process. Field meanings and how to free
space are in [DEPLOYMENT.md](DEPLOYMENT.md#storage).

```json
{
    "storage": {
        "generated_at": "2026-09-30T06:49:30.430626+00:00",
        "cache_seconds": 60,
        "disk": {
            "total_bytes": 494384795648,
            "used_bytes": 448495538176,
            "free_bytes": 45889257472,
            "free_fraction": 0.0928,
            "low": true
        },
        "database": {
            "bytes": 603080,
            "main_bytes": 409600,
            "wal_bytes": 160712,
            "shm_bytes": 32768
        },
        "media": {
            "files": 1,
            "bytes": 4096,
            "skipped_symlinks": 0,
            "unreadable_dirs": 0,
            "truncated": false,
            "categories": [
                {
                    "key": "networks",
                    "label": "Networks",
                    "files": 1,
                    "bytes": 4096,
                    "largest": [
                        {
                            "name": "AE0B26BB",
                            "bytes": 4096,
                            "detail": "Avalanche / demo-net-1",
                            "url": "/networks/Avalanche/"
                        }
                    ]
                },
                {
                    "key": "pgn_archives",
                    "label": "Archived PGNs",
                    "files": 0,
                    "bytes": 0,
                    "largest": []
                }
            ]
        },
        "networks_by_engine": [
            {
                "engine": "Avalanche",
                "networks": 1,
                "files": 1,
                "missing_files": 0,
                "bytes": 4096,
                "url": "/networks/Avalanche/"
            }
        ],
        "upload_spool": null
    }
}
```

(`categories` trimmed; it always holds all five: `networks`, `pgn_archives`,
`pgn_pending`, `event_logs`, `other`.)

| Error | Status | Body |
|---|---|---|
| Authentication failed | 401 | `{"error": "API requires authentication for this endpoint"}` |
| Throttled | 429 | `{"error": "Too many failed logins"}` |

### `POST /api/active/`

How many workloads a Client with a given `system_info` and blacklist would
choose among right now, using the same filters as `clientGetWorkload`. Used by
supervisors that start a worker only when there is work; see
[SUPERVISED_WORKERS.md](SUPERVISED_WORKERS.md) for how to obtain
`system_info` from the Client with `--print-system-info` instead of writing it
by hand.

| Field | Meaning |
|---|---|
| `username`, `password` | An enabled account. Required even when `require_login_to_view` is off. |
| `system_info` | JSON object with `concurrency`, `physical_cores`, `logical_cores`, `ram_total_mb`, `syzygy_max` (integers), `noisy` (boolean), `cpu_flags` (list), `os_name` (string), `compilers` and `tokens` (objects keyed by engine name), and optionally `focus` and `only` (lists). Types are checked exactly; other keys are ignored. |
| `blacklist` | Optional, repeatable. Workload ids to exclude, ASCII digits, at most 18. |

```
curl -s https://openbench.example/api/active/ \
  --data-urlencode "username=$OPENBENCH_USERNAME" \
  --data-urlencode "password=$OPENBENCH_PASSWORD" \
  --data-urlencode 'system_info={"concurrency":8,"physical_cores":8,"logical_cores":16,"ram_total_mb":32000,"syzygy_max":0,"noisy":false,"cpu_flags":["AVX2"],"os_name":"Linux","compilers":{"Avalanche":["zig","0.16.0"]},"tokens":{}}' \
  -d blacklist=1 -d blacklist=2
```

```json
{
    "assignable": 1
}
```

`assignable > 0` exactly when a registered Client with that `system_info` and
blacklist would be given work now. Nothing is written: no Machine, Result or
session is created.

| Error | Status | Body |
|---|---|---|
| Not `POST` | 405 | `{"error": "POST required"}` |
| Bad credentials, or account not enabled | 401 | `{"error": "Bad Credentials"}` |
| Missing or malformed `system_info`, or a bad `blacklist` entry | 400 | `{"error": "Malformed system_info or blacklist"}` |
| Throttled | 429 | `{"error": "Too many failed logins"}` |

## `/scripts/`

`POST /scripts/` is the form endpoint behind `Scripts/upload_net.py` and
`Scripts/create_test.py`. It is not JSON: it hands the request to the same
code as the website's forms and answers with a redirect or an HTML page. The
outcome is in the page's banner (`<div class="error-message">`,
`warning-message` or `status-message`), which the script has to follow the
redirect to see. `Scripts/upload_net.py` has a small parser for it.

Every call needs `username` and `password` of an enabled account in the POST
body, and an `action`. A browser session is ignored. On success the caller is
logged in: the response sets `sessionid` and `csrftoken` cookies (see
[CSRF](#csrf) for what that means for a reused `requests.Session`).

| Outcome | Answer |
|---|---|
| `Sec-Fetch-Site` is `cross-site` or `same-site` (a browser on another site) | 403, plain text, nobody is logged in |
| `GET` (or any method but `POST`) | 302 to `/login/`, no banner and no session |
| Missing, wrong or not-enabled credentials | 302 to `/login/`, banner "Unable to authenticate user" |
| Throttled | 302 to `/login/`, banner "Too many failed logins. Try again later" |
| Any `action` other than the two below | 302 to `/index/`, banner "Unknown scripts action" |

### `action=UPLOAD_NETWORK`

`multipart/form-data` with `engine`, `name` and the file as `netfile`. Only
Approvers may upload.

| Outcome | Answer |
|---|---|
| Uploaded | 302 to `/networks/<engine>/`, banner "Uploaded demo-net-1 for Avalanche" |
| Not an Approver | 302 to `/index/`, banner "Only Approvers may upload Networks" |
| `engine` or `name` missing or empty | 302 to `/networks/`, banner "UPLOAD_NETWORK requires engine" (or `name`, or both) |
| `netfile` missing | 302 to `/networks/`, banner "No network file was uploaded as netfile" |
| `name` outside `[a-zA-Z0-9_.-]` | 302 to `/networks/`, banner "Valid characters are [a-zA-Z0-9_.-]" |
| Same file already uploaded for that engine | 302 to `/networks/`, banner "Network with that hash already exists for that engine" |
| Name already used for that engine | 302 to `/networks/`, banner "Network with that name already exists for that engine" |
| Unknown engine | 302 to `/networks/`, banner "No Engine found with matching name" |

Side effects: creates a `Network` row authored by the caller, not the default,
and stores the file in `Media/<sha>` unless another engine already has it.
Make it the default from the website's Networks page.

### `action=CREATE_TEST`

Creates an engine test from the fields of the website's `/test/new/` form;
`Scripts/create_test.py` lists all of them with typical values (`dev_engine`,
`dev_repo`, `dev_branch`, `dev_bench`, `dev_network`, `dev_options`,
`dev_time_control`, the same `base_*` fields, `test_mode`, `test_bounds`,
`test_confidence`, `test_max_games`, `book_name`, `upload_pgns`, `throughput`,
`workload_size`, `priority`, `syzygy_wdl`, `syzygy_adj`, `win_adj`,
`draw_adj`, `scale_method`, `scale_nps`, `info`). Validation is described in
[WORKLOADS.md](WORKLOADS.md); it resolves both branches against GitHub, so the
server needs GitHub access. Only tests can be created this way, not tunes or
datagen.

| Outcome | Answer |
|---|---|
| Created | 302 to `/index/`; a warning banner if dev appears behind base |
| Rejected | 200, the create form with every reason in the error banner, one per line, for example "no-such-branch-xyz could not be found", or "Base Branch is required" for an empty branch, which is refused without asking GitHub |

A field left out of the POST is rejected like an invalid one, for example
`"Priority" is not an Integer`. A missing `dev_branch`, `dev_repo` or
`dev_engine` (or `base_…`) is reported as "Missing form fields: dev_branch",
and that side is not looked up on GitHub. `info` and the two `*_bench` fields
are optional.

Side effects: creates the test, its engines and a `CREATE` event. The test is
approved at once when the caller is an Approver and `use_cross_approval` is
off; otherwise it waits for an Approver. The response does not carry the new
workload's id; find it on `/index/` or `/user/<username>/`.

## `/health/`

`GET /health/` (or `HEAD`) needs no login and reveals nothing beyond whether
the database answers a query against the `OpenBench_serverstate` table.

```json
{"status": "ok"}
```

It answers 503 `{"status": "unavailable"}` when the query fails. Any other
method gets 405 with an empty body and `Allow: GET, HEAD`. The reverse proxy and
deployers use it, see [DEPLOYMENT.md](DEPLOYMENT.md).

## Client worker endpoints

The `/client*/` endpoints (`clientWorkerInfo`, `clientGetWorkload`,
`clientSubmitResults` and the rest in `OpenBench/urls.py`) are the internal
protocol between the Server and `Client/worker.py`. They change together with
the Client, which must run the version in `Config/config.json`
(`client_version`), so they are not a stable API and are not documented here.
`Client/worker.py` is the source of truth for their requests and responses;
the security rules they follow are in [SECURITY.md](SECURITY.md#workers).

## Scripting

A minimal script with [`requests`](https://requests.readthedocs.io/). It reads
the server and credentials from the environment, like the scripts in
`Scripts/`, and POSTs them with every call.

```python
import os
import requests

SERVER = os.environ["OPENBENCH_SERVER"].rstrip("/")
CREDENTIALS = {
    "username": os.environ["OPENBENCH_USERNAME"],
    "password": os.environ["OPENBENCH_PASSWORD"],
}


class OpenBenchError(RuntimeError):
    def __init__(self, response: requests.Response) -> None:
        self.status = response.status_code
        try:
            message = response.json()["error"]
        except (ValueError, KeyError):
            message = response.reason
        super().__init__(f"{self.status}: {message}")


def api(path: str, **fields: str) -> requests.Response:
    response = requests.post(f"{SERVER}/api/{path.strip('/')}/", data={**CREDENTIALS, **fields}, timeout=30)
    if not response.ok:
        raise OpenBenchError(response)
    return response


info = api("workload/3/info").json()["info"]
print(info["dev"]["name"], info["games"], info["currentllr"])

network = api("networks/Avalanche").json()["default"]
if network:
    with open(network["sha256"], "wb") as out:
        out.write(api(f"networks/Avalanche/{network['sha256']}").content)
```

`OpenBenchError.status` tells the cases apart: 401 means the credentials are
wrong or the account is not enabled, 403 that the account lacks a permission,
404 that the engine, network or workload does not exist, 409 that the request
may succeed later, and 429 that the throttle refused the login.

Notes for script authors:

- Use plain `requests.post`, or a fresh `requests.Session` per call, with
  credentials in the body. A session that went through `/scripts/` is logged
  in, and the delete endpoint then demands a CSRF token.
- Keep retries on 401 bounded: every failed password counts toward the
  throttle, and 10 failures lock that username out from your address for the
  rest of the 15-minute window. Do not retry a 429 before the window ends.
- `/scripts/` does not follow these rules; read its banner as
  `Scripts/upload_net.py` does.
