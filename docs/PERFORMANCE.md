# Query performance

Every page costs a fixed number of queries, whatever the number of Tests,
Results, Machines or LogEvents behind it. `OpenBench/tests/test_query_counts.py`
pins each budget with `assertNumQueries` at two dataset sizes built by
`OpenBench/tests/datasets.py`, so a change that adds a query per row fails
there first.

| Page | Queries |
|---|---|
| `/index/` (page 1), `/user/<name>/` | 9 |
| `/index/<n>/`, `/greens/` | 6 |
| `/search/` | 7 |
| `/events/`, `/errors/` | 7 |
| `/networks/` | 5 |
| `/test/<id>/` | 11 |
| `/api/workload/<id>/summary/`, `/results/` | 5 |
| `/api/workload/<id>/insights/` | 6 |
| `/api/workload/<id>/history.csv` | 5 |
| `/compare/?a=<id>&b=<id>` | 7 |
| `/api/insights/server/` | 11 |

Budgets include the session, user and Profile lookups every logged-in page
pays.

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
- **Events and errors** fetch the page's Tests in one `in_bulk` query and hang
  each on its LogEvent as `event.workload`.
- **The Profile** is fetched once per request, with its User, by
  `request_profile`, and cached on the request.
- **The Machine status** line sums concurrency and MNPS in SQL, reading
  `concurrency` out of the `info` JSON, instead of loading every online Machine.
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
| `network_engine_sha` | The dev Network annotation on every listed row, network lookups | correlated `SCAN network` per row → `SEARCH` | |

No index was added for Results by `(test, machine)`: the existing `test_id`
index already turns every Result filter into a `SEARCH`, and the remaining
per-machine filter costs well under a millisecond.

## HTML minification

django-htmlmin was removed; see [DEPLOYMENT.md](DEPLOYMENT.md#response-compression).
