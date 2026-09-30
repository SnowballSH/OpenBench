# Workload insights

The Server keeps a small time series of every Workload's cumulative counters,
and derives progress, throughput, time-left and strength statistics from it.
Two JSON endpoints expose the results for the workload page and the index.

Code lives in `OpenBench/insights/`:

| Module | Role |
|---|---|
| `domain.py` | Enums and frozen dataclasses shared by everything else (`Outcomes`, `ProgressPoint`, `WorkloadFacts`). |
| `recorder.py` | Writes `WorkloadSnapshot` rows from `update_test`, throttled and thinned. |
| `strength.py` | Elo with 95% interval, normalized Elo, LOS, draw ratio, pentanomial fractions. |
| `timing.py` | Elapsed time, overall and recent games per hour. |
| `sprt.py` | Per-pair LLR drift and variance, and the expected remaining steps of the SPRT. |
| `eta.py` | Remaining games per Workload mode, converted into time. |
| `series.py` | One chart point per snapshot. |
| `listing.py` | Time taken and time left for a listing row, from a few picked snapshots; see [Listings](#listings). |
| `grouping.py`, `contributions.py` | Per-machine and per-CPU contribution; `grouping.sum_by_key` also backs `fetch_result_summaries`. |
| `speed.py` | Nodes per second from node and millisecond counters, shared by the result summaries and the machine page. |
| `sources.py` | Reads a Workload's Test, snapshots and Results and turns them into domain values. |
| `workload.py`, `server.py` | Assemble the two payloads; `server.py` runs its own aggregate queries over Machines, Tests, snapshots and Profiles. |
| `serialize.py`, `api.py`, `views.py` | JSON conversion and the HTTP endpoints. |
| `export.py` | The history as CSV. |

Everything except `sources.py`, `recorder.py`, the loaders in `server.py`, and
the views is a pure function of domain values, and is unit tested without a
database (`OpenBench/tests/test_insights_analytics.py`).

## The `WorkloadSnapshot` model

| Field | Meaning |
|---|---|
| `test` | `ForeignKey(Test, CASCADE, related_name='snapshots')`, without its own index: the `(test, created)` index serves every lookup by Test. |
| `created` | When the snapshot was taken; indexed on its own and together with `test`. |
| `games`, `losses`, `draws`, `wins` | Cumulative trinomial counters, copied from the Test. |
| `LL`, `LD`, `DD`, `DW`, `WW` | Cumulative pentanomial counters, copied from the Test. |
| `llr` | `Test.currentllr` at that moment; `0.0` outside SPRT. |

Deletion: `Result` protects its `Test` (`PROTECT`) because Results are source
data. Snapshots are derived entirely from the Test's own counters, so they
`CASCADE`: they never block deleting a Test and never outlive one. Workloads
are normally only flagged `deleted`, which leaves the history in place.

## Recording

`OpenBench.utils.update_test` calls `record_snapshot_safely(test, games)` once,
at the end of its `transaction.atomic()` block, after the Test row is saved and
while it is still locked by `select_for_update()`, so a snapshot always agrees
with the Test counters it copies. `games` is the size of the batch just
reported. The call runs in a savepoint and logs (`OpenBench.insights.recorder`
logger) and swallows any exception, so a fault in the history can roll back
only the history, never a worker's results.

A snapshot is written when any of these hold:

- the Workload has no snapshot yet (the first report is always recorded);
- the newest snapshot is at least `SNAPSHOT_INTERVAL` (60 s) old;
- the report just finished the Workload (the finishing point is always kept).

When a Workload's first snapshot is recorded and the Test already held games
before this batch (it was running when this feature was deployed), an origin
snapshot with zero counters is written at `Test.creation` first. Without it the
whole earlier history would appear to have been played in the first minute:
elapsed time, rates and `games_last_24h` would all be wrong. With it those
earlier games are treated as played evenly since creation.

Cost on the hot path: one aggregate query over the `(test, created)` index,
returning the newest `created` and the row count. At most once a minute per
Workload, one `INSERT` follows (two for the origin case above, once per
Workload). When that insert would take the history past `SNAPSHOT_LIMIT`
(400 rows), the history is thinned in the same transaction: one indexed read of
`(id, created)` and one `DELETE ... WHERE id IN (...)`, which happens about once
every 140 inserts (a little over 2 hours of play).

Thinning (`thinned_ids`) never touches the last `RECENT_KEPT` (1 hour) of
snapshots, so the recent window used for rates is always at one-minute
resolution. The older snapshots are split into `SNAPSHOT_TARGET` (200) equal
time buckets between the first snapshot and the start of that hour; the newest
snapshot of each bucket and the first snapshot are kept. A thinned history
holds at most 201 older rows plus about 61 recent ones, then grows at one row a
minute until the next thinning, so a Workload never holds more than 400 rows.
Activity older than an hour coarsens to `span / 200`.

Manual stops (`modify_workload`) do not pass through `update_test`, so a stopped
Workload's newest snapshot may be up to a minute behind its counters. The
insights payload covers that gap with the live tail point described below.

## Timeline used by the analytics

- **Snapshots exist:** the snapshots in time order, starting with the origin
  snapshot at `Test.creation` when there is one. If the Test's counters are
  ahead of the newest snapshot (reports throttled within the last minute), a
  tail point is appended from the Test counters at `Test.updated`.
- **No snapshots, games > 0** (Workloads that ran before this feature): a single
  synthetic point from the Test counters at `Test.updated`, and
  `history.synthetic` is `true`. Rates treat the Workload as having played
  evenly from `Test.creation` to `Test.updated`.
- **No snapshots, no games:** an empty history; timing starts at creation.

`started_at` is the first snapshot's time: `Test.creation` for a Workload with
an origin snapshot or no history, otherwise the time its first batch was
reported, so the first batch's own duration is not counted. `ended_at` is the last timeline point for a
finished Workload, and `null` while it runs, in which case "now" ends every
window. A Workload that has stalled therefore shows a falling rate, and an ETA
of `null` once its recent rate reaches zero.

## Metrics

### Throughput

`games_at(t)` interpolates cumulative games linearly between timeline points,
and holds the last value after the last point.

- `overall.games_per_hour = 3600 · (games_at(end) − games_at(start)) / (end − start)`
- `recent.games_per_hour` is the same over `[end − 1 h, end]`, clamped to start
  no earlier than the first point.
- A window shorter than 5 minutes yields `null`. `window_seconds` reports the
  span actually used, so a young Workload's "recent" rate visibly covers less
  than an hour.

### Strength

All strength numbers use the pentanomial counters unless the Workload is
trinomial (`use_tri`), mirroring `Test.results()`. They are `null` for SPSA,
whose games are between perturbed copies of the same engine.

- `elo` is `OpenBench.stats.Elo`: the Student-t 95% interval of the mean
  score per pair (or per game), mapped through the logistic Elo curve.
  Undefined with fewer than 2 pairs.
- `normalized_elo`: with per-pair mean score `μ` and variance `σ²` over `N`
  pairs, `nElo = (800 / ln 10) · (μ − 0.5) / (σ · √2)` (no `√2` for trinomial),
  with interval `nElo ± 1.96 · (800 / ln 10) / (√2 · √N)`. This is the
  normalized Elo of the Fishtest pentanomial SPRT used by
  `OpenBench.stats.PentanomialSPRT`. `null` when `σ = 0`.
- `los` (likelihood of superiority) is `Φ((μ − 0.5) / (σ / √N))`, the normal
  approximation. When `σ = 0` it is `1`, `0`, or `0.5` by the sign of
  `μ − 0.5`.
- `draw_ratio` is draws / games from the trinomial counters.
- `penta_fractions` is each pentanomial bucket over the pair count.

### Time left

`eta.kind` says which rule produced the numbers:

| `kind` | When | `remaining_games` |
|---|---|---|
| `finished` | the Workload is finished | `0` (and `remaining_seconds` `0`) |
| `target` | GAMES, DATAGEN | `max(0, max_games − games)` |
| `target` | SPSA | `max(0, 2 · pairs_per · iterations − games)` |
| `sprt_estimate` | SPRT with enough data | expected games until either bound, below |
| `unavailable` | anything else | `null` |

`remaining_seconds = 3600 · remaining_games / rate`, where `rate` is the recent
games per hour, falling back to the overall rate. It is `null` when neither
exists or the rate is zero, and so is `completes_at` (now + `remaining_seconds`).

`eta.reason` says why the numbers are incomplete, and is `null` when they are
not (`finished`, or a count and a time were both computed; `completes_at` alone
can still be `null` when it would overflow the calendar):

| `reason` | `kind` | Meaning |
|---|---|---|
| `too_few_games` | `unavailable` | SPRT with fewer than 200 games |
| `outside_bounds` | `unavailable` | the LLR is not strictly between the SPRT bounds |
| `empty_outcome` | `unavailable` | trinomial SPRT with an empty W, D or L bucket |
| `no_variance` | `unavailable` | the LLR increment variance is below 1e-12 |
| `no_target` | `unavailable` | not SPRT, and no target number of games |
| `no_rate` | `target`, `sprt_estimate` | games are known but there is no non-zero rate to turn them into time |

`sprt_unavailable_reason` classifies the SPRT cases with the same tests and
thresholds as `forecast_sprt`; the tests assert both agree.

#### The SPRT estimate

This is an estimate that assumes the Workload keeps producing results like the
ones it has produced so far. Treat it as an order of magnitude, never a promise.

1. **LLR increment per pair.** Take the empirical pentanomial distribution
   `p̂` (with the same 1e-3 floor per bucket as `PentanomialSPRT`) and the two
   maximum-likelihood laws `p₀`, `p₁` for the bounds `elo0`, `elo1`, computed
   with `OpenBench.stats.MLE_tvalue` exactly as the SPRT does. The per-pair
   increment of the LLR takes the value `rᵢ = ln(p₁ᵢ / p₀ᵢ)` with probability
   `p̂ᵢ`. Its mean `μ = Σ p̂ᵢ rᵢ` is the drift, and satisfies `μ · N = LLR`
   exactly, so the drift is the Workload's own LLR trend. Its variance is
   `σ² = Σ p̂ᵢ (rᵢ − μ)²`. Trinomial Workloads do the same per game with the
   BayesElo laws of `TrinomialSPRT`.
2. **Expected exit time.** The LLR is approximated by Brownian motion with
   drift `μ` and variance `σ²` per step, starting at the current LLR `x`
   between the bounds `a < x < b`. With `k = 2μ / σ²`, the probability of
   leaving through `b` is `P = (1 − e^{−k(x−a)}) / (1 − e^{−k(b−a)})` and the
   expected number of steps is `E[T] = (P · (b − a) − (x − a)) / μ`
   (Wald's approximation). As `μ → 0` this tends to `(x − a)(b − x) / σ²`, which
   is used directly when `|k (b − a)| < 1e-9`, so a zero drift does not make the
   estimate undefined: a Workload sitting at Elo ≈ midpoint of the bounds still
   ends, just slowly.
3. `remaining_games = ceil(E[T]) · 2` (pairs to games; `· 1` for trinomial).

It is `unavailable` when there are fewer than 200 games, when the LLR is not
strictly between the bounds, when the increment variance is below 1e-12, or for
a trinomial Workload with an empty W, D or L bucket (where `TrinomialSPRT`
itself returns 0). Caveats: the drift is itself a noisy estimate early on; the
Brownian approximation ignores the overshoot of the final step past the bound,
so it slightly underestimates the remaining games; and the empirical `p̂` changes as the test runs, so the estimate
moves with it. The probability `P` is not reported: it is conditional on the
current Elo estimate being the truth, and reads as far more confident than the
LOS for the same data.

### Contributions

From the Workload's `Result` rows, grouped by Machine and by `cpu_name`
(missing names become `Unknown`):

- `games`, `pairs` summed; `share = games / all games of the Workload`;
- `pairs_per_hour = 3600 · pairs / timing.elapsed_seconds`: the average rate
  over the Workload's whole elapsed time (from `started_at`, so from creation
  for Workloads that were running at deploy), not over the time that machine
  was actually assigned, since Results carry no start time. For a Workload
  whose history starts at its first report, that first batch counts towards
  the pairs but not the elapsed time, which is negligible past the first few
  minutes;
- `elo` from the group's own summed counters, as above.

### Server

- `fleet`: Machines updated within the last 2 minutes, their summed
  `concurrency` as threads, and `Σ concurrency · mnps`, the same rule as
  `utils.getMachineStatus` on the index page.
- `workloads`: unfinished, non-deleted Workloads, split by `approved`, the same
  filters as `utils.get_pending_tests` and `get_active_tests`. `server.py`
  queries the models itself rather than importing `OpenBench.utils`, which
  would make importing `OpenBench.insights.api` circular.
- `games_last_24h`: for every Workload with a snapshot in the window, its
  current `Test.games` minus the games at the window start. The start value is
  interpolated between the newest snapshot before the window and the oldest one
  inside it, which keeps thinned histories accurate. A Workload with no earlier
  snapshot counts from zero; that is right, because its first snapshot is its
  first report, or else an origin snapshot at creation exists (see Recording). Reports within a minute of a Workload's last
  snapshot before the window can be missed.
- `finished_last_7d`: finished, non-deleted Workloads whose `Test.updated` is in
  the window. `Test.updated` also moves on later edits, so this is "finished
  and touched within 7 days". `completed` counts finished SPSA tunes that
  played all their iterations (`update_test` never marks a tune passed or
  failed), and `stopped` the remaining finished Workloads that neither passed
  nor failed. Both this count and a Workload's `completed` status use
  `domain.tune_completed`, which only ever applies to SPSA. The index strip
  names completed tunes only when there are any.
  `sprt_pass_rate = sprt_passed / (sprt_passed + sprt_failed)`, `null` without
  a decided SPRT.
- `top_contributors`: the ten Profiles with the most lifetime games.

## API

Both endpoints use `api_authenticate`: a logged-in browser session, or
`username` and `password` in a POST body. With `require_login_to_view` the user
must also be enabled. Numbers are JSON numbers (never formatted strings),
non-finite values become `null`, and timestamps are ISO-8601 with a UTC offset.
The Elo interval object is `{ "lower": number, "value": number, "upper": number }`
everywhere it appears; `Elo` below means that object or `null`.

### `GET|POST /api/workload/<id>/insights/`

Authentication failures and unknown ids follow the other `api/workload/`
queries: status 401 or 404 with `{ "error": "..." }`.

```jsonc
{
  "insights": {
    "generated_at": "2026-09-30T04:14:46.613352+00:00",
    "workload": {
      "id": 1,
      "mode": "SPRT",                 // SPRT | GAMES | SPSA | DATAGEN
      "status": "active",             // pending | active | passed | failed | completed | stopped | deleted
      "use_penta": true,
      "created_at": "...", "updated_at": "..."
    },
    "progress": {
      "games": 10400, "pairs": 5200,
      "trinomial": [1751, 6812, 1837],        // [losses, draws, wins]
      "pentanomial": [253, 1245, 2113, 1341, 248],
      "llr": 0.73, "llr_lower": -2.94, "llr_upper": 2.94,   // null outside SPRT
      "target_games": null,           // GAMES/DATAGEN max_games, SPSA 2·pairs_per·iterations
      "fraction": null                // games / target_games, capped at 1
    },
    "timing": {
      "started_at": "...",
      "ended_at": null,               // set once finished
      "elapsed_seconds": 210784.27,
      "overall": { "games_per_hour": 176.5, "window_seconds": 210784.27 },  // or null
      "recent":  { "games_per_hour": 190.5, "window_seconds": 3600.0 }      // or null
    },
    "eta": {
      "kind": "sprt_estimate",        // finished | target | sprt_estimate | unavailable
      "remaining_games": 31412,       // or null
      "remaining_seconds": 593589.2,  // or null
      "completes_at": "...",          // or null
      "reason": null                  // or a reason from the table above
    },
    "strength": {                     // null for SPSA
      "elo": Elo, "normalized_elo": Elo,
      "los": 0.898, "draw_ratio": 0.655,
      "penta_fractions": [0.049, 0.239, 0.406, 0.258, 0.048]   // or null
    },
    "history": {
      "synthetic": false,
      "points": [
        { "timestamp": "...", "games": 64, "llr": 0.089,          // llr null outside SPRT
          "elo": 43.7, "elo_lower": -21.2, "elo_upper": 111.7 }   // null for SPSA or < 2 pairs
      ]
    },
    "contributions": {
      "machines": [
        { "machine_id": 3, "machine_name": "demo-3",   // null when the Client sent none
          "owner": "lab-worker", "cpu_name": "Intel(R) Core(TM) i9-13900K",
          "stats": { "games": 2684, "pairs": 1342, "share": 0.258,
                     "pairs_per_hour": 22.9, "elo": Elo } }
      ],
      "cpus": [
        { "cpu_name": "AMD Ryzen 9 7950X 16-Core Processor", "machines": 2,
          "stats": { "games": 3296, "pairs": 1648, "share": 0.317,
                     "pairs_per_hour": 28.1, "elo": Elo } }
      ]
    }
  }
}
```

`history.points` is in time order, holds at most 401 entries (400 snapshots and
the tail point), and ends at the current counters. `machines`
and `cpus` are sorted by games, largest first. `share` and `pairs_per_hour` are
`null` when their denominator is zero.

### `GET|POST /api/insights/server/`

Returns status 401 with `{ "error": "..." }` when authentication fails.

```jsonc
{
  "server": {
    "generated_at": "...",
    "fleet": { "machines": 5, "threads": 162, "mnps": 340.8 },
    "workloads": { "pending": 1, "active": 3 },
    "games_last_24h": 14686,
    "finished_last_7d": {
      "window_days": 7, "total": 3, "passed": 2, "failed": 1, "completed": 0, "stopped": 0,
      "sprt_passed": 1, "sprt_failed": 1, "sprt_pass_rate": 0.5
    },
    "top_contributors": [ { "username": "lab-worker", "games": 8120 } ]
  }
}
```

Cost: the workload endpoint reads the Test, its snapshots and its Results once
each, and computes one Elo interval per history point (about 25 ms for 150
points; `OpenBench.stats.Elo` dominates). The server endpoint runs a fixed
number of aggregate queries.

### `GET|POST /api/workload/<id>/history.csv`

<a id="history-csv"></a>The same `history.points` as a CSV file
(`export.py`), with the same authentication and errors as the insights
endpoint; [API.md](API.md#getpost-apiworkloadidhistorycsv) has the headers.
It reads the Test and its snapshots and nothing else (no Results).

| Column | Value |
|---|---|
| `timestamp` | ISO-8601 in UTC with a `+00:00` offset |
| `games` | cumulative games |
| `llr` | the LLR at that point; empty outside SPRT |
| `elo`, `elo_lower`, `elo_upper` | the Elo estimate and its 95% interval; empty for SPSA or with fewer than 2 pairs |

Numbers are written with Python's shortest round-trip `repr`, and non-finite
values as empty cells. Every cell is built from a number or a timestamp, and
`cell_text` raises on anything else (a string, a boolean), so a cell can never
begin a spreadsheet formula: a leading `-` only ever starts a negative number.

## Where it shows

- **Workload page** (`Templates/OpenBench/workload.html`, `OpenBench/static/insights.js`):
  an Insights section below the configuration and actions and above the
  SPSA parameters, result summary and individual results. The configuration and
  stat block say what the workload is and where it stands; the insights explain
  how it got there; the raw tables stay last. The script fetches
  `/api/workload/<id>/insights/` once on load, then every 60 s while the
  Workload is `pending` or `active` (so a pending one picks up its approval),
  skipping ticks while the tab is hidden and refreshing as soon as it is
  visible again. A failed fetch shows an inline banner, keeps the last good
  render and retries with the delay doubling up to 8 minutes; after three
  consecutive client errors (a 4xx, or an `error` payload such as an unknown
  id) it stops. `eta.reason` picks the wording under an unavailable time left.
  - Progress tiles: elapsed, games (with the fraction of `target_games` when
    there is one), games per hour (recent window and overall), and time left.
    For SPRT the time left is labelled an estimate and prefixed with `≈`;
    `completes_at` is shown in local time.
  - Strength tiles, when `strength` is not null and games were played: LLR
    position between the bounds (SPRT), Elo and normalized Elo with their 95%
    intervals, LOS, draw ratio.
  - Charts from `history.points`: LLR against games with both bounds (SPRT),
    Elo with its 95% band against games (not SPSA), and cumulative games
    against elapsed time with the target line where there is one. Fewer than
    two points show a single notice instead.
  - Contributions: per-CPU and per-machine tables with a share bar, games,
    pairs per hour and Elo (not SPSA).
  - A **Download history (CSV)** link beside the Insights heading.
  - A **Compare with workload** form under the actions: a plain GET form to
    `/compare/` with `a` set to this workload and `b` typed in, so it works
    without JavaScript.
- **Index** (`Templates/OpenBench/index.html`): a strip of server tiles from
  `/api/insights/server/` (machines online with threads and MNPS, active and
  pending workloads, games in the last 24 h, Workloads finished in 7 days and
  the SPRT pass rate). It is hidden when the endpoint refuses the viewer. Active
  rows also carry a thin bar under the stat block, from the `workload_progress`
  template filter: the LLR's position between the SPRT bounds, or games over
  `max_games` for GAMES and DATAGEN. It reads only the Test's fields; SPSA rows
  have none, since their stat block already states iterations.

## Comparing workloads

`/compare/?a=<id>&b=<id>` shows two workloads side by side
(`OpenBench/compare/`, `Templates/OpenBench/compare.html`,
`OpenBench/static/compare.js`). Any two modes can be compared.

| Module | Role |
|---|---|
| `analysis.py` | Pure functions: parsing the query, the final Elo difference, and the difference series. |
| `present.py` | The summary rows and the chart data island. |
| `views.py` | The page. |

- **Access and validation.** The page goes through `render()`, so it follows
  `require_login_to_view`, and an anonymous viewer is redirected before any
  workload is looked up. With neither id the page shows only its form. An id
  that is not 1 to 18 digits, a missing id, or the same id twice answers 400
  with the form and the reasons. An id with no workload answers 404 with the
  form, still filled in, and "Workload N does not exist".
- **Reuse.** Each side is `insights.workload.insights_without_contributions`:
  the workload endpoint's payload built by the same `build_insights`, with no
  Result rows, since the page shows no contributions.
- **Summary table** (the text alternative for every chart): name (linking to
  the workload), mode, status, games (of the target), elapsed, games per hour
  and time left, with the workload page's wording. Elo and normalized Elo with
  their 95% intervals and the LOS appear only when both workloads have a
  strength (neither is SPSA); the LLR with its bounds appears when either is
  SPRT, with a dash for the other.
- **Elo(A) − Elo(B)**, below the table, from the two current Elo intervals:
  `d = elo_A − elo_B ± √(h_A² + h_B²)`, where `h` is half the width of each
  95% interval. It is labelled approximate: the two workloads are treated as
  independent samples, the Elo intervals are not exactly symmetric, and each
  workload measures its own dev against its own base, so the difference only
  means something when both share a base.
- **Charts**, from each side's `history.points`, with `--series-1` for A and
  `--series-2` (dashed) for B, and a legend since each chart but one has two
  series. The pair passes the categorical palette checks against `--surface`
  in both themes.
  - *Elo* against games, each with its 95% band, when both have a strength;
    the y range is fitted as on the workload page.
  - *Elo difference (A − B)* against games, in `--series-3` with its
    approximate band: at every game count either history has within the range
    both cover, each side's Elo and half-width are interpolated linearly
    between its neighbouring points, then combined as above.
  - *LLR* against games when both are SPRT, with the pass and fail bounds of
    each (drawn once when they agree).
  - *Games played* against the time since each workload's first history point.
  - The tooltip picks the nearest point of every series whose range covers
    the cursor, since the two series have points at different games.
- **Data island.** One `json_script` island, `compare-data`, holds each side's
  history as columns (`games`, `elapsed` in seconds from its first point,
  `llr`, `elo`, `elo_lower`, `elo_upper`, rounded to 3 decimals) and the
  difference series as columns (`games`, `value`, `lower`, `upper`). The page
  has no other inline script, handler or style.
- **Cost.** Seven queries whatever the history sizes: the session, the user,
  both Tests in one query (with `dev`, `base` and `spsa_run` joined), each
  Test's snapshots, and the Profile and engine list `render()` reads.
  `test_query_counts.py` pins it at two dataset sizes.

## Listings

Every row of the index, user, greens and search listings carries a line of
timing under its stat block (`Blocks/row_timing.html`, the `listing_timing`
filter):

- **Running** (approved, not finished): the time left and the recent games per
  hour, with the same rules and wording as the workload page's tiles. An SPRT
  estimate reads `≈ 3h 20m left`, is announced as "estimated", and carries the
  tile's caveat as a tooltip; GAMES, DATAGEN and SPSA targets read
  `3h 20m left`. When `eta.reason` is set the row shows that reason's text
  instead (`needs 200 games first`, `no recent throughput`, ...), except
  `no_target`, which shows only the rate. A zero rate is not shown.
- **Finished**: `took 5h 12m`, from the first snapshot (or `Test.creation`
  without one) to the finish time of the progress page
  (`OpenBench.progress.sources.finish_time`): the newest snapshot, or
  `Test.updated` for a Workload without snapshots.
- **Pending**: nothing.

The timing costs no query. `page_queries.listing_tests` annotates each row
with correlated subqueries on the `(test, created)` index: the first snapshot,
the finish time, and for unfinished rows the newest snapshot and the two
snapshots either side of the start of the recent window (`now − 1 h`); `now` is
annotated too, so the window and the time left use the same instant.
`games_at` only ever interpolates between a timestamp's two neighbours, and
the rates only ask about the first snapshot, the window start and now, so
these few snapshots and the Test's counters (the same tail rule as the
workload page) give exactly the `timing` and `eta` the insights endpoint would.
`OpenBench/tests/test_listing_timing.py` checks that against full histories.

Measured on SQLite with 20,000 Tests and a million snapshots: page 1 of the
finished list went from 1.2 to 2.6 ms and 25 rows at offset 2,475 from 2.7 to
4.0 ms; 5,714 active rows (far more than a real server runs) from 112 to
161 ms, about 9 µs a row.

## Demo data

`seed_demo` gives every seeded Workload with games a 150-point history between
its start and its finish (or now), with jittered arrival rates and LLRs
computed at every point, ending exactly at the Workload's counters. It also
credits each Profile with the games its Machines played, so the server
payload's `top_contributors` is populated.

Its SPSA tunes are played iteration by iteration with the same `c` and `r`
schedule as `spsa_workload_assignment_dict` and the Client's delta update, so
their parameters end at values the Server itself could have reached. Each
iteration is a mini-match between the two perturbed sides, and the side nearer a
hidden optimum plays slightly stronger, so the parameters drift towards it.

## Fleet pages

`OpenBench/fleet/` backs `/machines/`, `/machines/<id>/` and `/users/`.
`status.py` holds the pure rules, `machines.py`, `machine_detail.py` and
`users.py` each pair pure summaries with one loader.

- **Online** is the same rule as the server payload's `fleet`: a heartbeat
  within the last 2 minutes (`ACTIVE_MACHINE`). `/machines/?show=24h` and
  `?show=7d` also list Machines last seen within that window, marked offline.
  Supervisors register a Machine per workload, so a window can hold
  thousands: the table lists every online Machine plus the 200 most recently
  seen offline ones (`OFFLINE_LISTED`), and says so when it is cut. The tiles
  and the CPU table are SQL aggregates over every Machine in the window, so
  they never depend on the cut. Threads and MNPS (`Σ concurrency · mnps`)
  count online Machines only; `Games, last 24h` is `games_last_24h` above.
- **Lifetime games** is the sum of a Machine's `Result.games`, computed by a
  correlated subquery in the Machine query.
- **Workload** is `Machine.workload`: the current one while online, the last
  one once offline.
- **Machine detail** lists the newest 50 Results of the Machine by
  `Result.updated`. Elo is that Machine's own interval from its Result counters
  (pentanomial unless the Workload is trinomial, none for SPSA); NPS is
  `1000 · dev_nodes / dev_time`.
- **Users** lists Profiles with games, tests, approver rights or an online
  Machine. Last activity is the later of the newest heartbeat of the user's
  Machines and the newest Workload they authored; logins are deliberately left
  out, so the page does not reveal when someone last signed in. Machine
  figures come from one query grouped by owner and authored Workloads from one
  query grouped by author, merged in Python rather than correlated per row.

Each page runs a fixed number of queries whatever the row count
(`OpenBench/tests/test_fleet.py` asserts it).

## Engine progress

`/progress/` shows how the testing effort as a whole moved over a window of
time; `/progress/<engine>/` limits it to Workloads whose `dev_engine` is that
engine. The window is the `window` query parameter: `30d`, `90d` (the default),
`1y` or `all`. The page goes through `render()`, so it follows
`require_login_to_view` like every other page; an unknown `window` falls back
to the default, and `/progress/?engine=X` redirects to `/progress/X/`. An
engine name containing `/` cannot travel in the path, so its links keep the
query form (`/progress/?engine=A%2FB`) and it is not redirected; a blank engine
in the path (`/progress/%20/`) redirects to `/progress/`. An engine with no
Engine configuration (enabled or not) is a 404, before any report is built or
cached. Reports are cached for 60 seconds per engine and window (the default
local-memory cache, so per process), which bounds the cost of repeated loads
and of the API; since only configured engines reach the cache, a caller cannot
fill it with arbitrary names.

Code lives in `OpenBench/progress/`:

| Module | Role |
|---|---|
| `domain.py` | `Window`, the source rows and the frozen report dataclasses. |
| `analysis.py` | Pure functions: parsing, the window scope, cumulative Elo, games per day, weekly series, rankings, the summary. |
| `sources.py` | The six aggregate queries below. |
| `report.py` | Runs the queries and assembles a `ProgressReport`. |
| `present.py` | Formats the report for the template (tiles, table rows, links). |
| `views.py` | The page and the JSON endpoint. |

`OpenBench/tests/test_progress.py` covers the pure functions without a
database, the queries against a small fixture, the page and the API.

### Window

A window of `n` days covers the last `n` UTC calendar days including today,
from midnight UTC `n − 1` days ago. `all` starts at the earliest day with data:
the first day with recorded games, the first green, or the Monday of the first
week with a finished SPRT test. Every series is bucketed by UTC day or UTC week
(weeks start on Monday), and the first week of a window is usually partial.

### Finish time

`Test` has no finish timestamp. For every finished test the finish time is its
newest snapshot's `created`: `update_test` always records the report that
finishes a Workload, and a manual stop leaves the last report as the newest
snapshot, so later edits (stop, delete, restore, modify), which move
`Test.updated`, do not move it. A stopped test's finish time is therefore its
last report, up to a minute before its final counters (see Recording), not the
moment someone stopped it. Only a test without snapshots falls back to
`Test.updated`. The query first narrows on `updated ≥ since − 1 h` (the
`test_completed_updated` index; the finish time is never later than `updated`
plus the moment between saving the Test and recording its snapshot), then
filters on the finish time.

### Metrics

- **Greens** are the index's definition: finished, not deleted, SPRT,
  `passed`, and `elolower + eloupper ≥ 0`. Blue (non-regression) passes are
  left out. With an engine selected, `dev_engine` must match; cross-engine
  tests are not excluded otherwise.
- **Cumulative Elo** is `Σ elo_i` over the greens in finish order, where
  `elo_i` is the point estimate of `OpenBench.stats.Elo` on the test's
  pentanomial counts (trinomial when `use_tri`), the same number the test page
  shows. Tests with fewer than two pairs (or trinomial games) have no
  estimate, add nothing and are counted in the "without an estimate" part of
  the tile. This sum is an estimate and
  overstates progress: an SPRT stops when its LLR crosses the upper bound, so
  the estimate of a passed test is biased upwards (selection bias), and each
  test measures a patch against its own base, so the terms are not measured
  against a common reference. Treat the line as a trend of accepted work, not
  as a rating.
- **SPRT outcomes per week**: finished, non-deleted SPRT tests, by the week of
  their finish time. `passed` and `failed` are the flags; `stopped` is
  finished with neither. Tunes, GAMES and DATAGEN Workloads are not SPRT, so
  they never count. The pass rate is `passed / (passed + failed)`, `null`
  without a decided test.
- **Games per day** comes from `WorkloadSnapshot`. For each Workload and UTC
  day, the day's largest cumulative `games` is taken; the games played on a day
  are that value minus the Workload's previous value, which for the first day in
  the window is the largest value before the window (zero if none). Deleted
  Workloads count, since their games were played. Caveats: a snapshot's games
  land on the day of that snapshot, so the games between the last snapshot
  before midnight and the first after it all count on the later day (thinned
  histories keep one snapshot per `span / 200`, so up to a few hours for long
  Workloads); a Workload that was running when history recording was deployed
  has an origin snapshot with zero games at its creation, so its earlier games
  all land on the day of its first recorded report; Workloads that finished
  before history recording have no snapshots and are not counted; a report
  within the last minute that has not been snapshotted yet is missing.
- **Top contributors**: `Result.games` summed by Machine owner over Results
  whose `updated` is in the window. A Result is cumulative per Machine and
  Workload, so a Result that started before the window counts in full; for
  short windows over long Workloads this overstates. The ten largest are
  listed, with their share of the window's total.
- **Top authors**: Workloads of any mode created in the window (not deleted),
  counted by `author`; the ten largest are listed with their share.
- **Tiles**: the Elo sum and the number of greens; SPRT tests finished with
  passed / failed / stopped; the pass rate; games played and the mean per day
  over the window's days; Workloads created and their distinct authors; users
  whose Machines reported games.

### Cost

Six queries whatever the data size (five for `all`, which needs no baseline),
each an aggregate or a bounded row set: the greens (one row per green, with a
correlated newest-snapshot subquery served by the `(test, created)` index), the
weekly outcome counts (grouped in SQL), the daily snapshot maxima (grouped by
Workload and day in SQL), the per-Workload baseline before the window (grouped
in SQL), games grouped by owner, and Workloads grouped by author. The daily
grouping uses SQLite's built-in `date(created)` rather than `TruncDate`, whose
SQLite implementation calls a Python function per row (about four times slower
on a 1.7M-snapshot history); with `USE_TZ` and `TIME_ZONE = 'UTC'` the stored
text is UTC, so both give the same day (a test checks this around midnight).
Other databases use `TruncDate(..., tzinfo=UTC)`. No Result or
snapshot row is loaded individually. The page adds the enabled engine names
and the queries `render()` always makes. `test_progress.py` asserts the counts
at two data sizes.

### `GET|POST /api/progress/?engine=&window=`

Authenticated with `api_authenticate`, like the other `api/` endpoints: a
logged-in browser session, or `username` and `password` in a POST body (the
query parameters stay in the URL). Returns status 401 with `{ "error": "..." }`
when authentication fails, and status 400 with `{ "error": "..." }` for a
`window` other than `30d`, `90d`, `1y` or `all`. The value is matched ignoring
case and surrounding whitespace, and an empty or omitted `window` means `90d`.
An empty or missing `engine` means every engine; names are trimmed and cut to
64 characters, and a name with no Engine configuration is status 404 with
`{ "error": "..." }`. Days are
`YYYY-MM-DD` (UTC) and timestamps ISO-8601 with a UTC offset.

```jsonc
{
  "progress": {
    "generated_at": "2026-09-30T12:00:00+00:00",
    "engine": "Avalanche",            // or null for every engine
    "window": "90d",                  // 30d | 90d | 1y | all
    "start": "2026-07-03",            // first day of every series
    "end": "2026-09-30",              // today, UTC
    "summary": {
      "elo_gained": 41.7,             // Σ of the greens' Elo point estimates
      "greens": 7,
      "greens_without_elo": 0,        // greens that added nothing
      "sprt": { "passed": 8, "failed": 5, "stopped": 1 },
      "sprt_pass_rate": 0.615,        // or null
      "games": 612430,
      "games_per_day": 6804.8,        // games / days, or null
      "days": 90,
      "tests_created": 16,
      "authors": 3,
      "contributors": 2
    },
    "elo_steps": [                    // the cumulative line, oldest first
      { "finished_at": "2026-07-01T09:12:44+00:00",
        "cumulative_elo": 5.2,
        "greens": 1 }                 // how many greens the sum covers
    ],
    "greens": [                       // the newest 500 greens, oldest first
      { "id": 42, "name": "lmp-table",
        "finished_at": "2026-07-01T09:12:44+00:00",
        "games": 24300,
        "elo_bounds": [0.0, 3.0],     // [elolower, eloupper]
        "elo": { "lower": 1.9, "value": 5.2, "upper": 8.5 },   // or null
        "cumulative_elo": 5.2 }       // running sum including this test
    ],
    "greens_omitted": 0,              // older greens left out of "greens"
    "weekly_outcomes": [              // every week from start's Monday, zeros included
      { "week_start": "2026-06-29", "passed": 1, "failed": 0, "stopped": 0 }
    ],
    "daily_games": [                  // every day from start to end, zeros included
      { "day": "2026-07-03", "games": 0 }
    ],
    "top_contributors": [             // at most 10, most games first
      { "username": "lab-worker", "games": 402110, "share": 0.657 }
    ],
    "top_authors": [                  // at most 10, most Workloads first
      { "username": "admin", "tests": 9, "share": 0.5625 }
    ]
  }
}
```

`share` is `null` when the window's total is zero. Long windows on a busy
server can hold thousands of greens, so `greens` carries only the newest 500
(every one of them is still in the summary and the sum) and `elo_steps` is
thinned to at most 500 points: with more greens than that, it keeps evenly
spaced greens by rank, always the first and the last, each with its exact
running sum, so the line keeps its shape at a coarser step. The page embeds
this same object (without the `progress` wrapper) as a `json_script` data island.

### The page

`Templates/OpenBench/progress.html` renders the tiles and every table on the
server; `OpenBench/static/progress.js` only draws the charts from the data
island and submits the engine form when the selection changes (its button
stays for visitors without scripts). The share bars carry `data-share`, which
`site.js` copies into `--share` like every other page. The page follows the
site's Content-Security-Policy (`docs/SECURITY.md`): its only inline `<script>`
is the `application/json` data island, which is never executed, and it has no
inline event handler or `style` attribute. `OpenBench/tests/test_csp.py` scans
the template and the rendered `/progress/` pages.

- **Cumulative Elo from greens**: a stepped line over time from `elo_steps`
  (linear axis in milliseconds with ticks on UTC day multiples), one marker per
  step, a zero line, and a caption stating that the sum is an estimate that
  overstates. The tooltip names the test, its Elo interval, games and date when
  the step's green is among those sent, and always the running sum.
- **SPRT outcomes per week**: stacked columns, `--pass` for passed, `--fail`
  for failed and `--neutral-edge` for stopped (result colours, as on the test
  lists), with a legend, a 2 px surface gap between segments and the pass rate
  in the tooltip. The pass rate for the window is a tile rather than a second
  axis.
- **Games per day**: columns in `--series-1`.
- Both bar charts have a table view in a `<details>` below them. The greens
  table lists the newest 100 greens with their Elo, bounds, games and the
  running sum; the contributors and authors tables show share bars.
- Charts follow the rules in `docs/UI.md`: colours from tokens at render time,
  re-rendered on theme change, no animation under reduced motion, `role="img"`
  canvases whose `aria-label` states the totals.

`seed_demo` adds eighteen finished SPRT tests spread over the last six months
(passed, failed, stopped and one non-regression pass, from all three demo
authors), so every window of the page has data.
