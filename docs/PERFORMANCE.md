# Query performance

Every page costs a fixed number of queries, whatever the number of Tests,
Results, Machines or LogEvents behind it. `OpenBench/tests/test_query_counts.py`
pins each budget with `assertNumQueries` at two dataset sizes built by
`OpenBench/tests/datasets.py`, so a change that adds a query per row fails
there first.

| Page | Queries |
|---|---|
| `/index/` (page 1), `/user/<name>/` | 9, plus 6 when an active row has no rate, plus 1 when a stopped workload is listed |
| `/index/<n>/`, `/greens/` | 6, plus 1 when a stopped workload is listed |
| `/search/` | 7 |
| `/go/?q=` (the header's quick jump; the header itself adds none) | 3 to 6 |
| `/api/jump/?q=` | at most 7 |
| `/events/`, `/errors/?view=list` | 7 |
| `/errors/` (grouped), with any filter | 9 |
| `/api/errors/` | 8 |
| `/event/<id>/` | 8 |
| `/networks/` | 5 |
| `/test/<id>/` | 11, 12 for a stopped workload, 17 for an active one; plus 1 once a game was played, for the summary's time left and rate ([UI.md](UI.md#workload-page)) |
| `/api/workload/<id>/summary/`, `/results/` | 5 |
| `/api/workload/<id>/insights/` | 6, or 12 for an active workload |
| `/api/workload/<id>/history.csv` | 5 |
| `/api/workload/<id>/games/` | 4 without PGN uploads, 5 with an up-to-date analysis; see [INSIGHTS.md](INSIGHTS.md#storage-and-cost) |
| `/compare/?a=<id>&b=<id>` | 7 |
| `/api/insights/server/` | 11 |
| `/machines/`, `/machines/?show=…` | 11 (9 before any snapshot exists in the last 24 hours, and 8 with no listed workload: empty lookups are skipped) |
| `/machines/<id>/` | 11 |
| `/users/` | 8 |
| `/api/workloads/` | 4, plus 6 with an active row, plus 1 with a stopped one |
| `/api/live/workloads/`, `/api/live/workload/<id>/` | 4 while the caller's token still matches; otherwise at most 13 and 10 |

Budgets include the session, user and Profile lookups every logged-in page
pays. The six extra queries are the workload diagnosis
([INSIGHTS.md](INSIGHTS.md#workload-diagnosis)), which judges every active
workload from one shared read of the fleet; `test_diagnosis.py` pins that
count at both dataset sizes.

## Where the per-row queries went

- **Test listings** (index, user, greens, search) go through
  `OpenBench.page_queries.listing_tests`, which joins `dev`, `base` and
  `spsa_run`, and annotates two values the summary row needs: the name of the
  dev Network (`dev_network_label`) and the Tune's parameter count
  (`spsa_parameter_count`). The `prettyDevName` and `shortStatBlock` filters
  use those annotations when present and query only when handed a bare Test,
  as the workload page does.
  The same query also annotates the snapshots and finish time behind each
  row's time taken or time left (see [INSIGHTS.md](INSIGHTS.md#listings)),
  so that line adds no query either.
- **Pending and Active tests**, and the Machine status, are only shown on the
  first page of a listing, so later pages no longer fetch them.
- **Events and errors** fetch the page's Tests in one query
  (`triage.groups.load_workloads`, with the dev Network annotation the row
  title needs) and pass each row its Test. The grouped errors page
  ([INSIGHTS.md](INSIGHTS.md#worker-errors)) costs five queries of its own
  whatever the number of events: one `GROUP BY (test_id, summary)` over the
  error events, the Tests of those groups, the last result time of each, the
  reporting registrations of the 25 groups on the page, and their Machines.
  The workload page's "Worker errors (N)" line is one `COUNT`.
- **The Profile** is fetched once per request, with its User, by
  `request_profile`, and cached on the request.
- **The Machine status** line sums concurrency and MNPS in SQL, reading
  `concurrency` out of the `info` JSON, instead of loading every online Machine.
  It counts each host once, by its current session
  ([INSIGHTS.md](INSIGHTS.md#hosts)): two correlated `EXISTS` probes of
  `machine_host_updated` per online registration.
- **The fleet pages** group registrations into hosts in three queries (current
  sessions in the window, per-host session totals, per-host games), then
  fetch the listed Workloads in one.
- **Search** is paged like the index (`/search/<page>/?<params>`), newest
  first, instead of rendering every match.

On the worker side, `clientSubmitResults` writes only the Test columns it
changes and takes the Profile's user from the verified Machine, and
`clientGetWorkload` reads both EngineConfigs in one query.

## Indexes

SQLite compiles a boolean filter to the bare column (`WHERE "finished"`),
which an ordinary index on that column cannot serve. A partial index whose
condition is the same expression can, and it only holds the rows the listing
reads. Each index below replaced a full table scan in `EXPLAIN QUERY PLAN`,
measured with 20,000 Tests, 50,000 LogEvents and 100,000 PGNs:

| Index | Serves | Plan before → after | Time before → after |
|---|---|---|---|
| `test_completed_updated`: `updated DESC WHERE finished AND NOT deleted` | Finished listings, their counts, the server insights' finished-since window | `SCAN test` + temp B-tree sort → `SCAN test USING INDEX` / `SEARCH (updated>?)` | page 15.0 → 0.4 ms, count 6.8 → 0.2 ms, window 7.4 → 0.5 ms |
| `test_unfinished`: `approved WHERE NOT finished AND NOT deleted` | Pending and Active listings, `clientGetWorkload`'s candidates, server insights' counts | `SCAN test` → `SCAN test USING INDEX test_unfinished` | counts 6.5 → 0.6 ms |
| `test_author` | `/user/<name>/` | `SCAN test` → `SEARCH USING INDEX (author=?)` | count 7.0 → 0.4 ms |
| `logevent_machine` | `/events/` count and page | `SCAN logevent` → `SEARCH USING COVERING INDEX (machine_id=?)` | count 1.7 → 0.5 ms |
| `pgn_unprocessed`: `test_id WHERE NOT processed` | The PGN watcher's batch, `/api/pgns/<id>/` | `SCAN pgn` + temp B-tree sort → `SCAN/SEARCH USING INDEX` | watcher 5.1 → 0.7 ms, api 3.8 → 0.01 ms |
| `logevent_test_machine`: `(test_id, machine_id)` | The diagnosis' worker errors of the active workloads, the newest event of each stopped one, and every error-triage query that names a workload (`/errors/?workload=`, the page's reporters, an event's occurrences, the workload page's count) | `SCAN logevent` → `SEARCH USING INDEX logevent_test_machine (test_id=? AND machine_id>?)`; newest event: `SEARCH USING COVERING INDEX logevent_test_machine (test_id=?)` | |
| `network_engine_sha` | The dev Network annotation on every listed row, network lookups | correlated `SCAN network` per row → `SEARCH` | |

### Error triage needs no index of its own

Measured on SQLite with 60,000 LogEvents (48,000 of them worker errors over
2,000 workloads), `EXPLAIN QUERY PLAN` shows every triage query on an
existing index:

| Query | Plan | Time |
|---|---|---|
| Grouped errors: `GROUP BY test_id, summary` over `machine_id > 0`, newest 501 | `SEARCH USING INDEX logevent_test_machine (ANY(test_id) AND machine_id>?)`, temp B-trees for the grouping and the order | 93 ms |
| The same for one workload (`?workload=`) | `SEARCH USING INDEX logevent_test_machine (test_id=? AND machine_id>?)` | under 1 ms |
| Flat list page (`ORDER BY id DESC LIMIT 25`) | reverse `SCAN` of the table that stops after 25 matches | under 1 ms |
| Flat list count | `SEARCH USING COVERING INDEX logevent_machine (machine_id>?)` | under 1 ms |
| Flat list count with a `kind` filter | the kind is a regular expression over `summary`, so every error event is read and matched | 130 to 255 ms, by kind |
| Reporting registrations of the page's workloads | `SEARCH USING INDEX logevent_test_machine (test_id=? AND machine_id>?)` | under 1 ms |
| Workload page count, an event's occurrences | `SEARCH USING (COVERING) INDEX logevent_test_machine` | under 1 ms |

A `kind` filter costs the same full read on the grouped page. Adding
`workload=` narrows either to that workload's rows first.

The unfiltered grouping reads every error event, so it grows with the table:
about 2 ms per thousand events. It and the kind-filtered count are the
queries here that are not bounded by a page; if the table reaches millions of rows, bound it to a time
window rather than adding an index, since no index avoids the aggregation.

### `Machine.host_key`

Grouping registrations into hosts could be done at query time, by extracting
the identifying fields from `Machine.info`. It is stored instead, in an
indexed column filled at registration, because every grouped read would
otherwise parse every registration's JSON. Measured on SQLite with 50,000
Machines of 400 hosts (the largest with 1,270 registrations) and 133,000
Results:

| Query | From `info` at query time | With `host_key` and `machine_host_updated` |
|---|---|---|
| Registrations of one host | `SEARCH … (user_id=?)`, then JSON per row: 13.1 ms | `SEARCH USING COVERING INDEX (host_key=?)`: 1.4 ms |
| Group every registration by host | `SCAN` + temp B-tree for `GROUP BY`: 635 ms | `SCAN USING COVERING INDEX`: 7.3 ms |
| Is this the host's current session? | not expressible without the scan above | two `SEARCH USING COVERING INDEX (host_key=? AND updated>?)` |

`machine_host_updated` is `(host_key, updated)`: it serves the host lookup,
covers the per-host session count and first-seen aggregate, and answers the
current-session probes without touching the table. With it, at that size:
`/machines/` 9 ms online, 180 ms for 24 hours and 300 ms for 7 days (all 400
hosts, dominated by summing every Result of the listed hosts for lifetime
games); `/machines/<id>/` 45 ms for the largest host; the index status line
and the server payload's `fleet` 1.7 ms each.

`machine_user_updated` is `(user, updated)`. The registration-time prune
reads the owner's newest 500 stale registrations through it
(`SEARCH USING INDEX machine_user_updated (user_id=? AND updated<?)`, plus a
covering `SEARCH` of the Result index per row): 2.0 ms for an owner holding
45,000 of 50,000 registrations, and 13.5 ms including the delete of 328 rows.
It also covers the `/users/` last-heartbeat aggregate, which was a table scan
(131 ms → 14 ms).

The cost is one `sha256` at registration and a 32-character column. The
migration keys existing rows in batches of 500.

No index was added for Results by `(test, machine)`: the existing `test_id`
index already turns every Result filter into a `SEARCH`, and the remaining
per-machine filter costs well under a millisecond.

## HTML minification

django-htmlmin was removed; see [DEPLOYMENT.md](DEPLOYMENT.md#response-compression).
