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
| `grouping.py`, `contributions.py` | Per-host and per-CPU contribution; `grouping.sum_by_key` also backs `fetch_result_summaries`. |
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

From the Workload's `Result` rows, grouped by host and by `cpu_name`
(missing names become `Unknown`). A host is one physical machine: every
registration it made (see [Hosts](#hosts)) is pooled into one `machines` row,
so its games add up and its Elo comes from the pooled counters rather than from
any one registration. `machine_id` is the host's newest registration that
played this Workload, which `/machines/<id>/` resolves to the host;
`machine_label` is the name with long ids shortened and `pool` its pool label
(see [Pools](#pools)), which the table shows instead of a bare UUID;
`registrations` keeps the per-registration games and pairs, newest first. A
CPU's `machines` counts hosts.

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

- `fleet`: hosts with a heartbeat within the last 2 minutes, the `concurrency`
  of each host's newest registration summed as threads, and
  `Σ concurrency · mnps`, the same rule as `utils.getMachineStatus` on the
  index page. A Client that restarts leaves its previous registration looking
  online for up to 2 minutes; counting registrations would count that machine
  twice.
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
        { "machine_id": 9, "machine_name": "demo-3",   // null when the Client sent none
          "machine_label": "demo-3", "pool": "demo-3",   // "batch-a0741747…:0", "batch-*"
          "owner": "lab-worker", "cpu_name": "Intel(R) Core(TM) i9-13900K",
          "registrations": [ { "machine_id": 9, "games": 1340, "pairs": 670 },
                             { "machine_id": 8, "games": 1344, "pairs": 672 } ],
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
`status.py` and `hosts.py` hold the pure rules, `sessions.py` the shared
queryset filters, `machines.py`, `machine_detail.py` and `users.py` each pair
pure summaries with one loader, and `housekeeping.py` removes registrations
that were never used.

### Hosts

The Client registers a new `Machine` row every time it starts, and a
supervisor running it with `--single-workload` starts it once per workload, or
once a minute while there is no work. A `Machine` row is therefore a
*registration* (the pages call it a session), and the pages group
registrations into *hosts*, one per physical machine.

`OpenBench.fleet.hosts.host_key(owner, info)` is the grouping rule. It is a
pure function of the owner's username and the registration `system_info`
(any mapping; no ORM), and returns a 32-hex digest. The name comes first:

| The Client reported | Key is the digest of |
|---|---|
| a `machine_name` (`--identity`; the Client sends `None` otherwise) | owner, name, hardware. The MAC is ignored. |
| no name, a usable `mac_address` | owner, MAC, hardware |
| neither | owner, hardware |

Hardware is `cpu_name`, `os_name`, `logical_cores` and `physical_cores`.

- **Owners never merge**: the username is part of every form.
- **Why the name outranks the MAC**: inside a container `uuid.getnode()` finds
  no interface and invents a random node on every start. On the live server
  150 registrations carried 149 different addresses, every start of one named
  job a new one. It sets the multicast bit only when the random value happens
  to, so about half of those look like real addresses.
- **Slots are separate hosts.** Supervisors run several Clients at once on one
  VM as `batch-<uuid>:0`, `:1`, …; each is counted with its own threads, they
  share a pool (below), and only a re-registration under the same name is
  folded into its host.
- A MAC is usable when it is 1 to 12 hex digits, non-zero, with the multicast
  bit clear.
- `concurrency` and the Client and OS versions are left out, so `-T` and
  upgrades do not split a host.
- The digest hides names and MACs; it is not exposed in JSON.

Limits, none of which lose data (the registrations and Results are intact, and
each registration is listed on the host page):

- Two machines of one owner with the same `--identity` and identical hardware
  are one host. Names are the operator's to keep distinct.
- An unnamed Client in a container gets a random address per start, so about
  half of its starts become hosts of their own. Name it.
- Two unnamed Clients on identical hardware merge when they share a real MAC
  (two Clients on one machine, cloned VMs) or both have none; while both are
  online they count as one machine with the threads of the newer registration.
- One machine splits when its key inputs change: a new `--identity`, a dual
  boot into another OS, SMT toggled in firmware, or, unnamed, a replaced
  network card.

The key is stored in `Machine.host_key`, filled by `Machine.save()` and, for
rows that predate it, by migration `0019`, which carries its own frozen copy
of the rule (a later change to the rule needs a new migration to re-key rows).
Storing it is what makes the grouping affordable; see
[PERFORMANCE.md](PERFORMANCE.md#indexes). Registration is otherwise unchanged:
every start still gets a new Machine id and secret.

A row can have an empty key: an image from before the column existed, run
again after a rollback, inserts Machines without it
([DEPLOYMENT.md](DEPLOYMENT.md#rolling-back-past-the-host-key)). An empty key
never groups: `current_sessions` treats each such row as a host of its own,
the insights payload computes the key from `info`, and the fleet pages key up
to 200 such rows before reading. Rows are also keyed by their own next
heartbeat or workload request, by any registration, and by
`prune_machines --apply`.

### Pools

Dozens of one-off hosts would bury the list, so `fleet/pools.py` adds a
display grouping above hosts. `pool_label(machine_name, cpu_name)` collapses
the volatile parts of a name: a UUID, a hex run of 8 or more characters that
contains a digit, a run of 4 or more digits, and a trailing `:n` slot each
become `*`, and adjacent `*` merge.

| `machine_name` | Pool label | Short name |
|---|---|---|
| `batch-a0741747-7d81-49a2-b96e-4b14d4c304c3:0` | `batch-*` | `batch-a0741747…:0` |
| `i-0f21e8465a561ec9e` | `i-*` | `i-0f21e846…` |
| `node00123` | `node*` | `node00123` |
| `demo-3`, `ip-10-0-12-34` | unchanged | unchanged |
| none | the CPU name | |

A pool is (owner, label, CPU name). It is purely presentational: nothing is
stored, no count of machines uses it, and hosts stay the unit everywhere else.
`short_name` keeps the first 8 characters of a UUID or of a hex run of 16 or
more, for labels.

`sessions.current_sessions(queryset)` keeps, of each host, the registration
with the newest heartbeat (ties go to the highest id). Everything that counts
machines goes through it.

### Pages

- **Online** is the same rule as the server payload's `fleet`: a heartbeat
  within the last 2 minutes (`ACTIVE_MACHINE`). `/machines/?show=24h` and
  `?show=7d` also list hosts last seen within that window, marked offline. A
  host is online when any of its registrations is. The table lists every
  online host plus the 200 most recently seen offline ones (`OFFLINE_LISTED`),
  and says so when it is cut. The tiles and the CPU table are computed over
  every host in the window, so they never depend on the cut. Threads and MNPS
  (`Σ concurrency · mnps`) count online hosts only, each by its current
  session; `Games, last 24h` is `games_last_24h` above.
- **Offline hosts roll up by pool.** Online hosts are always listed one per
  row. Offline hosts that share a pool become one row with the pool label, the
  number of machines, and their sessions and games summed, first seen the
  earliest and last seen the latest; a pool of one host stays an ordinary row.
  The pool's name links to `?pool=<key>` (a 12-hex digest of the pool), which
  lists that pool's hosts individually. The 200-row cut counts rows, so a pool
  uses one. The roll-up happens in Python over the hosts the page already
  loaded: the query count is the same with or without `pool`.
- **A row** takes its name, hardware, threads, MNPS, workload and last
  heartbeat from the host's current session, read out of `info` in SQL so no
  blob is deserialized. **Sessions** is the number of registrations that
  exist for the host, **First seen** the oldest last-heartbeat among them
  (a `Machine` has no creation time), and **Games** the sum of `Result.games`
  over all of them. Never-used registrations are removed by housekeeping
  ([DEPLOYMENT.md](DEPLOYMENT.md#machine-registrations)), so Sessions
  describes the rows that exist, not every start the Client ever made.
- **Workload** is the current session's `Machine.workload`: the current one
  while online, the last one once offline. A supervised host that is polling
  for work is online and idle.
- **Machine detail** (`/machines/<id>/`) takes any registration id and shows
  its host; that registration's row is highlighted (`aria-current`) in the
  Sessions table, which lists the newest 25 and always includes the selected
  one. Hardware and software come from the current session. Workloads lists
  the newest 50 by `Result.updated`, one row per Workload with the counters
  of every registration pooled: Elo is the interval of the pooled counters
  (pentanomial unless the Workload is trinomial, none for SPSA), NPS is
  `1000 · Σ dev_nodes / Σ dev_time`, and Sessions counts the registrations
  that played it.
- **Users** lists Profiles with games, tests, approver rights or an online
  host. Machines and Threads count online hosts by their current session.
  Last activity is the later of the newest heartbeat of the user's
  registrations and the newest Workload they authored; logins are deliberately
  left out, so the page does not reveal when someone last signed in.

Each page runs a fixed number of queries whatever the row count
(`OpenBench/tests/test_fleet.py` and `test_query_counts.py` assert it).

## Engine progress

`/progress/` shows how an engine moved over a window of time;
`/progress/<engine>/` limits it to Workloads whose `dev_engine` is that
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

The page has three parts. The **lineage** answers "how much stronger did the
engine get, and through which commits"; **economics & speed** answers "what
did that cost, how fast does the trunk move, and did the engine search slower
as it got stronger"; the **activity** charts answer "how much testing
happened".

Code lives in `OpenBench/progress/`:

| Module | Role |
|---|---|
| `domain.py` | `Window`, `TimeClass`, the source rows and the frozen report dataclasses: the contracts everything else is written against. |
| `conditions.py` | Classifies a test's time control and thread count into a `TimeClass`. |
| `lineage.py` | Pure graph functions: steps, pooling, the trunk, branches, chained series, direct checks, the window. |
| `speed.py` | Pure functions for search speed: the dev-over-base ratio of a step from per-host node counters, its interval, the chained series. |
| `economics.py` | Pure functions over the window's trunk and candidates: cost buckets, the per-class table, games per Elo, cadence. |
| `options.py` | Reads `Threads` out of an options string (apart from `conditions.py` so the queries can use it without importing `utils`). |
| `analysis.py` | Pure functions for the activity half: parsing, the window scope, games per day, weekly series, rankings, the summary. |
| `sources.py` | The queries below. |
| `report.py` | Runs the queries and assembles a `ProgressReport`. |
| `present.py` | Formats the report for the template (tiles, table rows, links). |
| `views.py` | The page and the JSON endpoint. |

`OpenBench/tests/test_progress_lineage.py` covers the graph functions on
hand-built graphs without a database, and `test_progress_economics.py` does
the same for speed, cost and cadence (missing counters, a single host, zero
time); `test_progress.py` covers the activity
functions, the queries against a small fixture, the page and the API.

### Why a lineage and not a sum

The page used to add up the Elo estimate of every passed SPRT. That is not a
measure of anything: a change tested at STC and again at LTC was counted
twice, a repeated test was counted once per repeat, and candidates that were
tested side by side against the same base were stacked as if one had been
built on the other. Tests are not independent increments; they are
measurements of the edges of a commit graph, and only the edges along one path
can be chained.

### The model

A test measures `dev` against `base`. Each side is a **commit**: the
`Engine.sha` the test was built from, together with the network it ran
(`dev_network` / `base_network`, empty for an engine without one), so a new
network on unchanged code is a commit of its own.

- **Runs** are the non-deleted `SPRT` and `GAMES` tests whose two sides are the
  same engine, in any state (pending, running, finished). Tunes and datagen
  sessions are not runs. A run whose base and dev are the same commit (a sanity
  run, or a test of options only) measures no change and is left out.
- A **step** is one `(base, dev)` pair. Every run of the pair belongs to the
  one step.
- Within a step, runs are grouped by **time-control class** into
  **measurements**: one per class, however many runs it took.
- A step's subject is the first non-blank line of `info` (the commit subject;
  later lines may carry tooling metadata) of its first run that has one.

#### Time-control classes

`conditions.time_class` decides, from the two time controls and the two
`Threads` options:

| Class | Rule |
|---|---|
| `stc` | Fischer control (`base+inc`), the same on both sides, one thread on both sides, base time under 20 s |
| `ltc` | the same, base time from 20 s up to but not including 120 s |
| `vltc` | the same, base time of 120 s or more |
| `smp` | Fischer control, the same on both sides, the same number of threads on both sides and more than one, at any base time |
| `other` | everything else: fixed nodes, depth or move time, cyclic controls (`moves/base+inc`), sides with different controls or thread counts, a missing `Threads` option |

So `8.0+0.08` is STC and `40.0+0.40` is LTC. Hash is not part of the rule: by
convention it follows the time control, and a class is meant to answer "which
kind of test was this", not to fingerprint every setting. A measurement lists
its runs with their exact time controls, so a class that mixes `8+0.08` and
`10+0.1` is visible as such. `other` lumps unlike conditions together, so it is
shown in the table but never chained.

#### Pooling repeats

Runs of the same pair at the same class are **pooled**: their pentanomial
counts are added and one Elo estimate with its 95% interval is computed from
the sum by `OpenBench.stats.Elo`, the same function behind the number on a
test page. Game pairs from separate runs of the same two builds under the same
conditions are independent draws from one distribution, so the summed counts
are the sufficient statistic and the pooled interval is the correct, narrower
one. Pooling also avoids the bias of picking one run: a failed SPRT followed
by a passing repeat is, pooled, closer to the truth than the passing run
alone, though each run still stopped on its own rule. If any run in the pool
recorded only trinomial results (`use_tri`), the pool is computed from the
trinomial counts of all of them, and `pooling` says which was used.

Only **finished** runs are pooled (passed, failed, stopped, or a completed
GAMES run). A run that is still going, or awaiting approval, is listed but adds
no games while any run of the measurement has finished. A measurement none of
whose runs has finished is **provisional**: it shows the estimate so far,
marked as such, and is never part of a chained total (below).

An estimate needs a sample: a pool of fewer than 30 pairs (or games, for
trinomial), or one whose results all fall in a single outcome, has no estimate
(`elo` is `null`). Two won pairs would otherwise read as +1200 Elo with a
four-point margin. The same rule applies to each run's own `elo`.

The measurement's **verdict** is the status of its newest decided run
(`passed` or `failed`, SPRT only). Without a decided run it is the first of
`running`, `pending` (awaiting approval), `completed` (a finished GAMES run,
whose `passed` flag only says who scored more) and `stopped` (an SPRT finished
without a verdict) that any run has.

### The trunk

Steps form a graph over commits. Usually it is a chain with side branches,
because each accepted dev becomes the base of the next test.

1. **One parent per commit.** A commit may have been tested against several
   bases (its predecessor, and later an old release). Its parent is the base
   that gives it the longest chain back to a root; ties go to the step tested
   first. Depths are computed over the graph with cycle-closing steps removed:
   a depth-first search visits passed steps before the others and older steps
   before newer, so of `B→A` (failed) and `A→B` (passed) it keeps `A→B`
   whichever was tested first. The steps to a commit from its parent are the
   **forest**; every other step is either a direct check or detached, see
   below.
2. **Taken steps.** A step is taken when the chain continues from it (some
   step in the forest uses its dev as base), or when it is a tip that passed:
   at least one of its measurements has the verdict `passed` and none has
   `failed`. Continuation says the commit was built on, not that its test
   passed: a failed step that was continued anyway is on the trunk with its
   failed badge and its (negative) estimate in the chain. The tile therefore
   counts "trunk steps", not accepted ones.
3. **The tree.** The forest may have several roots. The lineage shown is the
   tree with the most taken steps; ties go to the most games, then to the
   newest test. A passing test between two commits that link to nothing can
   therefore not replace an established chain. Other trees that have taken
   steps are named in a notice above the table (`others`), and their tests are
   in the detached list.
4. **The head.** From the tree's root, follow taken steps forward while there
   are any. Where several are taken, a step the chain continues from beats a
   tip that merely passed, and among equals the one with the newest test in its
   subtree wins. Where that walk ends is the head. If no step is taken, the
   trunk is empty and the head is the root.
5. **The trunk** is the path from the root to the head. Its steps are numbered
   from 1; commit 0 is the root.

Rule 4 means the trunk follows the line the newest work builds on. A stray
test against an old commit does not cut it short, since the walk passes that
commit; a passed candidate on an old base does not displace it either, since a
line that continues beats an uncontinued tip. The trunk moves to another
branch once something is tested on top of that branch and it is the more
recently active.

Everything else is placed relative to the trunk:

- **Candidates** are the forest steps that branch off a trunk commit without
  being on the trunk, with the steps that descend from them (each carries its
  `depth`, 1 for a direct branch). They are parallel or abandoned work: listed
  above the commit they branched from, never added to anything.
- **Direct checks** are steps outside the forest whose base and dev are both
  on the trunk, base first: a regression or progression run of a later commit
  against an older one.
- **Detached** steps are the rest: other trees (a base that never links to the
  trunk), cross-branch comparisons, cycle-closing steps. An instance that tests
  moving branch names rather than pinned commits produces mostly detached
  steps, because a merged branch's SHA is not the SHA the next test uses as
  base. The lineage needs dev commits to reappear as bases.

### Chained estimate

For one class, along trunk steps `1..n` with pooled estimates `e_i` and 95%
intervals `[l_i, u_i]`, over the steps that have a finished measurement with an
estimate at that class:

```
value   = Σ e_i
margin  = sqrt( Σ h_i² ),   h_i = (u_i − l_i) / 2
chained = value ± margin
```

Variances of independent estimates add, so the half-widths add in quadrature.
Each class is its own series. STC and LTC are never added together, and a step
with no measurement at a class is a **gap**: it contributes nothing, its point
has no cumulative value, the line breaks there and resumes from the same sum,
and the series says how many steps it covers (`measured` of `steps`). Nothing
is borrowed from another class. A **provisional** step (its tests at that class
are all still running) is not in the sum either: its point carries `projected`,
the chain so far plus its estimate so far, which the chart draws as a dashed
segment to a hollow marker, and the tile names it ("1 running") without moving
the total. Half-width means `(upper − lower) / 2` everywhere, in the tiles, the
table, the chart and the JSON-derived tooltips.

Caveats, stated on the page:

- **Selection.** Steps are on the trunk mostly because their tests passed, and
  an SPRT stops when its LLR crosses a bound, so the estimate of a passed test
  is biased upwards. The chained value overstates.
- **Additivity.** Elo differences add exactly only under the logistic model
  with the same opponents; a step measured against its predecessor says nothing
  exact about the pair twelve steps apart.
- **Conditions.** Books, exact time controls and worker hardware differ
  between steps within a class.

Read it as a trend. A **direct check** is the independent control: one run
measures the whole span, with none of the chaining. The page puts it next to
the chained estimate over the same steps at the same class, with the
difference `direct − chained ± sqrt(h_direct² + h_chained²)`; when the chained
side does not cover every step of the span, its coverage says so.

### Window

A window of `n` days covers the last `n` UTC calendar days including today,
from midnight UTC `n − 1` days ago. `all` starts at the earliest day with
activity data: the first day with recorded games or the Monday of the first
week with a finished SPRT test. Every activity series is bucketed by UTC day or
UTC week (weeks start on Monday), and the first week of a window is usually
partial.

The graph is always built from every run of the engine, whatever the window,
since the trunk cannot be found from a slice. The window then selects a
**suffix of the trunk**: the steps after the last one whose `measured_at` (the
newest finish time among its runs, or the creation time of a run still going)
is before the window. The chain shown is therefore contiguous, sums start from
zero at the commit before the first shown step (`origin`), and step numbers
stay those of the whole trunk.

### Finish time

`Test` has no finish timestamp. For every finished test the finish time is its
newest snapshot's `created`: `update_test` always records the report that
finishes a Workload, and a manual stop leaves the last report as the newest
snapshot, so later edits (stop, delete, restore, modify), which move
`Test.updated`, do not move it. A stopped test's finish time is therefore its
last report, up to a minute before its final counters (see Recording), not the
moment someone stopped it. Only a test without snapshots falls back to
`Test.updated`. The weekly query first narrows on `updated ≥ since − 1 h` (the
`test_completed_updated` index; the finish time is never later than `updated`
plus the moment between saving the Test and recording its snapshot), then
filters on the finish time.

### One engine

A lineage follows one engine's commits. With an engine selected it is that
engine's. On `/progress/` (every engine) it is shown when exactly one engine
has steps; with several, the page lists them (`lineage_engines`) and
`lineage` is `null`. Cross-engine tests are not runs.

### Activity metrics

- **SPRT outcomes per week**: finished, non-deleted SPRT tests, by the week of
  their finish time. `passed` and `failed` are the flags; `stopped` is
  finished with neither. Tunes, GAMES and DATAGEN Workloads are not SPRT, so
  they never count. The pass rate is `passed / (passed + failed)`, `null`
  without a decided test. With an engine selected, `dev_engine` must match.
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
- **Tiles**: the chained Elo at STC and at LTC over the window's trunk steps,
  each with its margin, its coverage and the number of steps still running (a
  dash when no step has a finished measurement at that class; the tile's edge is
  green for a positive total and red for a negative one); trunk steps in the
  window and the candidates that branched from them; measurements (pooled step-and-class cells of those steps
  and candidates) and the runs behind them; the SPRT pass rate; games played
  and the mean per day. No tile adds classes together or counts a commit twice.

### Search speed

"Is the engine getting slower as it gets stronger?" Every `Result` carries
four counters the worker adds up from the PGN move comments of the games it
played: nodes searched and milliseconds thought, for dev and for base
(`dev_nodes`, `dev_time`, `base_nodes`, `base_time`). Both engines play the
same games on the same machine, so their nodes-per-second ratio is a paired
measurement that the machine's own speed cancels out of.

- **Unit.** One observation per **host** (`Machine.host_key`, the physical
  machine or batch job, see Fleet pages) per step: the counters of all its
  Results on the step's runs are added, then
  `x = ln( (dev_nodes / dev_time) / (base_nodes / base_time) )`. Only Results
  with all four counters above zero count (a Result with no counters, or zero
  time, is left out entirely, games included), and only runs of a chained
  class: `other` mixes unlike conditions, so it is not used.
- **Step ratio.** The weighted mean of `x` over hosts, weighted by the host's
  thinking time `w = dev_time + base_time`; `ratio = exp(mean)`. A step whose
  counted games number fewer than 30 has no speed.
- **Interval.** With `n ≥ 2` hosts, the variance of the weighted mean is
  estimated from the host-to-host scatter,
  `n / (n − 1) · Σ w²(x − mean)² / (Σ w)²`, and the 95% half-width is its root
  times the Student t quantile for `n − 1` degrees of freedom, applied in log
  space. It is rough: hosts are few, and it assumes each host is an independent
  draw. With one host there is no interval (`lower` and `upper` are `null`).
- **Classes.** The step's speed pools every chained class. The same ratio is
  also computed per class (`speed.classes`); when two classes that both have
  intervals differ by more than their margins in quadrature, `classes_differ`
  is true and the lineage row shows the classes next to the pooled figure.
  Otherwise only the pooled figure is shown.
- **Chain.** Along the window's trunk the log ratios add (the ratios
  multiply): the cumulative ratio at step `k` is the engine's speed relative to
  the window's origin commit, shown as a percentage change. Log half-widths add
  in quadrature, like the Elo chain. A step without speed is a gap that adds
  nothing (`measured` of `steps`). Once a step measured on a single host is in
  the product, the cumulative interval is `null` from there on (`unbounded`
  counts such steps): a band that left their uncertainty out would be too
  narrow.

Running tests count: node counters are not selected by a stopping rule, so a
running test's speed is as valid as a finished one's. The figure is nodes per
second, not time to depth: a change that searches fewer nodes for the same
result is not "faster" here, and a slower engine that passes its test has paid
for its slowdown in the Elo the test measured. `Engine.bench` (the node count
of the engine's bench run) is shown per step for the same sanity purpose: a
dev bench equal to the base's says the commit did not change the search
("same as base"), a different one confirms a functional change.

PR #42 (`result-insights`) adds counter helpers to `OpenBench/insights/speed.py`;
this page does not depend on them and keeps its ratio maths in
`OpenBench/progress/speed.py`.

### Testing economics

Everything here is over the **window's steps**: its trunk steps and the
candidates that branched from them (and from the origin), all of them, not only
the ones the payload lists.

Per step (`step.cost`), over every run of the step at every class, finished or
not:

- `games`: the runs' games.
- `decision_seconds`: for each finished run, its finish time minus its first
  report (the first snapshot, or creation for a test without history), added
  up. It is time under test, not elapsed time: queueing before the first report
  is excluded, and two runs that overlapped count twice. `null` while no run
  has finished.
- `core_hours`: **search time**. The sum of `dev_time + base_time` over the
  step's Results that have counters, times the run's `Threads`, in hours. At
  one thread only one engine thinks at a time, so this is the core time the
  engines spent searching. It is not machine uptime: engine start-up, the
  book, adjudicated tails and idle workers are not in it, and it is unscaled
  (real milliseconds on whatever hardware played). `null` when no Result has
  counters; `counted_games` says how many games it covers. An estimate from
  games × time control was rejected: it needs a guess at moves per game and at
  how much of the clock is used, and the counters are measured.

Window summaries (`economics`):

- **Buckets** `trunk`, `failed`, `other`: the trunk steps; candidates with a
  `failed` verdict at any class; the remaining candidates (still running,
  stopped, or passed but not built on). Each has steps, runs, games, search
  core-hours, and its share of the window's games and core-hours. "Spent on
  failed changes" is the failed bucket's share of search time, with its share
  of games beside it; without counters it falls back to games. A trunk step's
  failed repeats count with the trunk: the bucket is the change's fate, not
  the run's. `counter_coverage` is counted games over games.
- **By class** (`classes`): decided SPRT runs `passed` and `failed`, the pass
  rate `passed / (passed + failed)`, the median games of a passed run and of a
  failed run, and all games and core-hours at the class (GAMES runs and
  undecided runs included in those two).
- **Games per Elo**: all games at the class in the window (trunk and
  candidates, so the failures are paid for) divided by the chained Elo of the
  window's trunk at that class; `null` unless the chained value is positive.
  The chained value is biased upwards by selection (see Chained estimate), so
  the figure is optimistic; the tile says so, and says when the chained total
  is within its margin of zero.

### Cadence

- A trunk step **joins** at the finish of its newest passed run; a step that
  was built on without a pass joins at its newest finish. `weekly` counts joins
  per UTC week over the window (zeros included).
- `steps_per_week` is joins × 7 divided by the days from the later of the
  window's start and the day the window's first trunk step was first tested,
  to today. On a young instance the rate is therefore over the time the
  lineage has existed, not over an empty 90 days.
- **First test to accepted**: for steps with a passed run, newest pass finish
  minus the creation of the step's first run (queueing included). Median and
  mean.
- **STC pass to LTC pass**: for steps passed at both, the first LTC pass that
  finished after the first STC pass, minus that STC pass. Median.

### Cost

Seven queries whatever the data size (six for `all`, which needs no baseline),
each an aggregate or a bounded row set: the usage (Results of the engine's
runs grouped by test and host, one row per pair, summing games and the four
counters), the runs (one row per SPRT or GAMES
test of the engine, joined to its two `Engine` rows for the SHAs and benches, with
correlated newest- and oldest-snapshot subqueries served by the `(test, created)` index), the
weekly outcome counts (grouped in SQL), the daily snapshot maxima (grouped by
Workload and day in SQL), the per-Workload baseline before the window (grouped
in SQL), games grouped by owner, and Workloads grouped by author. The whole
graph is built in memory from the one runs query, never a query per commit;
the graph functions are linear in runs apart from sorting (an iterative depth-first search, so a
chain of thousands of steps does not recurse). The daily
grouping uses SQLite's built-in `date(created)` rather than `TruncDate`, whose
SQLite implementation calls a Python function per row (about four times slower
on a 1.7M-snapshot history); with `USE_TZ` and `TIME_ZONE = 'UTC'` the stored
text is UTC, so both give the same day (a test checks this around midnight).
Other databases use `TruncDate(..., tzinfo=UTC)`. No Result or
snapshot row is loaded individually. The page adds the enabled engine names
and the queries `render()` always makes. `test_progress.py` asserts the counts
at two data sizes.

The payload is bounded: the newest 500 trunk steps of the window
(`steps_omitted` counts the rest, which still count in the series totals), the
newest 50 candidates per trunk commit (`candidates_omitted`) and the newest 50
detached steps (`detached_omitted`). The page lists the newest 100 steps.

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
`YYYY-MM-DD` (UTC) and timestamps ISO-8601 with a UTC offset. Every Elo
estimate is `{ "lower", "value", "upper" }` (a 95% interval) or `null`.

```jsonc
{
  "progress": {
    "generated_at": "2026-09-30T12:00:00+00:00",
    "engine": "Avalanche",            // or null for every engine
    "window": "90d",                  // 30d | 90d | 1y | all
    "start": "2026-07-03",            // first day of the activity series
    "end": "2026-09-30",              // today, UTC
    "summary": {
      "lineage": {
        "trunk_steps": 6,             // trunk steps in the window
        "candidates": 5,              // steps branching off them, not on the trunk
        "measurements": 17,           // pooled (step, class) cells of both
        "runs": 18                    // tests behind those cells
      },
      "sprt": { "passed": 8, "failed": 5, "stopped": 1 },
      "sprt_pass_rate": 0.615,        // or null
      "games": 612430,
      "games_per_day": 6804.8,        // games / days, or null
      "days": 90,
      "tests_created": 16,
      "authors": 3,
      "contributors": 2
    },
    "lineage": {                      // or null: no steps, or several engines and none chosen
      "engine": "Avalanche",
      "classes": ["stc", "ltc"],      // classes measured in what is listed, in fixed order
      "origin": { "sha": "8c308d43…", "network": "" },   // commit before the first shown step
      "origin_candidates": [],        // candidates that branched from the origin
      "origin_candidates_omitted": 0,
      "head": { "sha": "741d6dc4…", "network": "" },
      "trunk_length": 6,              // steps on the whole trunk, whatever the window
      "steps": [                      // the window's trunk steps, oldest first
        { "index": 1,                 // position on the whole trunk, from 1
          "step": {
            "base": { "sha": "8c308d43…", "network": "" },
            "dev": { "sha": "76f2da3c…", "network": "" },
            "repo": "https://github.com/SnowballSH/Avalanche",
            "subject": "Tune the LMR table",          // Test.info
            "author": "admin",
            "first_run": 5,                           // lowest workload id of the step
            "first_tested_at": "2026-08-22T10:00:00+00:00",
            "last_tested_at": "2026-08-23T10:00:00+00:00",   // newest run created
            "measured_at": "2026-08-23T16:41:07+00:00",      // newest finish, or creation while running
            "base_bench": 3271955,    // Engine.bench of each side, null when unknown (0)
            "dev_bench": 3133448,
            "speed": {                // dev over base nodes per second, or null without counters
              "pooled": { "ratio": 0.9807, "lower": 0.9797, "upper": 0.9816,   // bounds null on one host
                          "hosts": 5, "games": 69100 },                        // games with counters
              "classes": [ { "time_class": "stc", "speed": { /* as pooled */ } } ],
              "classes_differ": false },
            "cost": { "runs": 3, "games": 69100,
                      "decision_seconds": 85680.0,     // finished runs, first report → finish; or null
                      "core_hours": 1156.5,            // search time; or null without counters
                      "counted_games": 69100 },
            "measurements": [         // one per class, in class order
              { "time_class": "stc",  // stc | ltc | vltc | smp | other
                "verdict": "passed",  // passed | failed | running | pending | completed | stopped
                "games": 29800,       // the pooled (finished) runs
                "pooling": "pentanomial",              // or trinomial
                "provisional": false, // true while no run has finished: elo is "so far"
                "elo": { "lower": 1.06, "value": 3.70, "upper": 6.34 },
                "runs": [             // oldest first
                  { "id": 5, "mode": "SPRT",           // SPRT | GAMES
                    "status": "passed",                // as verdict
                    "time_control": "8.0+0.08",
                    "created_at": "2026-08-22T10:00:00+00:00",
                    "finished_at": "2026-08-22T18:12:30+00:00",   // null until finished
                    "games": 29800,
                    "elo": { "lower": 1.06, "value": 3.70, "upper": 6.34 },
                    "started_at": "2026-08-22T10:03:10+00:00",    // first report, or creation
                    "counted_games": 29800,            // games in Results with node counters
                    "core_hours": 212.4 }              // or null
                ] }
            ]
          },
          "candidates": [             // branched from this step's dev, depth-first
            { "depth": 1, "step": { /* as above */ } }
          ],
          "candidates_omitted": 0 }
      ],
      "steps_omitted": 0,             // older window steps left out of "steps" and "points"
      "series": [                     // one per chained class measured on the shown trunk
        { "time_class": "stc",
          "points": [                 // one per step in "steps"
            { "index": 1,
              "elo": { "lower": 1.06, "value": 3.70, "upper": 6.34 },         // the step, or null
              "cumulative": { "lower": 1.06, "value": 3.70, "upper": 6.34 }, // null at a gap or a provisional step
              "projected": null }     // provisional step: the chain so far plus its estimate so far
          ],
          "total": { "lower": 18.2, "value": 25.8, "upper": 33.4 },   // whole window, or null
          "measured": 6,              // steps with a finished estimate at this class
          "provisional": 0,           // steps whose estimate is still provisional
          "steps": 6 }                // trunk steps in the window
      ],
      "direct": [                     // runs spanning several trunk steps
        { "time_class": "ltc",
          "base": { "sha": "8c308d43…", "network": "" },
          "dev": { "sha": "ea33d777…", "network": "" },
          "repo": "https://github.com/SnowballSH/Avalanche",
          "first_index": 1,           // first and last trunk step spanned
          "last_index": 5,
          "direct": { /* a measurement, as above */ },
          "chained": { "lower": 9.6, "value": 14.9, "upper": 20.2 },   // same span and class, or null
          "measured": 4,              // spanned steps the chained side covers
          "steps": 5 }
      ],
      "detached": [ /* steps, as above, newest first */ ],
      "detached_omitted": 0,
      "others": [                     // other trees that have taken steps
        { "root": { "sha": "f45c…", "network": "" }, "steps": 1, "taken": 1 }
      ]
    },
    "economics": {                    // null whenever lineage is null
      "trunk":  { "steps": 3, "runs": 7, "games": 157200,
                  "core_hours": 2520.2,       // search time, see Testing economics
                  "counted_games": 157200,    // games behind core_hours
                  "games_share": 0.803, "core_share": 0.903 },   // of the three buckets; null when the total is zero
      "failed": { /* as trunk: candidates with a failed verdict */ },
      "other":  { /* as trunk: the remaining candidates */ },
      "counter_coverage": 1.0,        // counted games / games, or null
      "classes": [                    // classes tested in the window, in class order
        { "time_class": "stc",
          "passed": 4, "failed": 1,   // decided SPRT runs
          "pass_rate": 0.8,           // or null
          "median_games_to_pass": 24750.0,    // or null
          "median_games_to_fail": 36800.0,    // or null
          "games": 146000, "core_hours": 1018.9,   // every run at the class
          "chained_elo": { "lower": 7.7, "value": 12.0, "upper": 16.3 },   // the window's trunk, or null
          "games_per_elo": 12191.8 }  // games / chained value; null unless that is positive
      ],
      "speed": {
        "points": [                   // one per step in lineage.steps
          { "index": 1,
            "step": { "ratio": 0.9941, "lower": 0.9911, "upper": 0.9971, "hosts": 5, "games": 47500 },   // or null
            "cumulative": { "ratio": 0.9941, "lower": 0.9911, "upper": 0.9971 } }   // null at a gap
        ],
        "total": { "ratio": 0.9437, "lower": 0.9400, "upper": 0.9474 },   // whole window, or null
        "measured": 3,                // steps with a speed
        "unbounded": 0,               // of those, steps on one host (no interval; total bounds are then null)
        "steps": 3 },
      "cadence": {
        "weekly": [ { "week_start": "2026-09-28", "steps": 2 } ],   // every week of the window
        "joined": 3,
        "steps_per_week": 3.5,        // or null
        "acceptance_samples": 3,
        "median_acceptance_seconds": 77100.4,     // first test created → newest pass, or null
        "mean_acceptance_seconds": 77100.4,
        "confirmation_samples": 2,
        "median_confirmation_seconds": 72000.6 }  // STC pass → LTC pass, or null
    },
    "lineage_engines": ["Avalanche"], // engines that have steps
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

`share` is `null` when the window's total is zero. A direct check is included
when its dev lies in the window; its chained side is summed over the whole
span even where that reaches before the window. The page embeds this same
object (without the `progress` wrapper) as a `json_script` data island. The
shape replaced the earlier `greens` / `elo_steps` / `summary.elo_gained`
fields, which nothing else consumed. `economics`, and the `base_bench`,
`dev_bench`, `speed` and `cost` of a step and `started_at`, `counted_games`
and `core_hours` of a run, were added later; no earlier field changed.

### The page

`Templates/OpenBench/progress.html` renders the tiles and every table on the
server; `OpenBench/static/progress.js` only draws the charts from the data
island and submits the engine form when the selection changes (its button
stays for visitors without scripts). The trunk chart has trunk step numbers on
its x axis (`s1`, `s2`, so they cannot be mistaken for workload ids), one line per class in a fixed colour per class (`--series-1` STC,
`--series-2` LTC, `--series-3` VLTC, `--series-4` SMP) with its band, and a
legend; the lineage table below it is its data table (`aria-details`), newest
step first, with candidates above the commit they branched from, a verdict
badge per measurement and a link to every underlying workload and to the
GitHub commit and compare pages (only for an `https://` repository). The share
bars carry `data-share`, which `site.js` copies into `--share` like every other
page. The page follows the site's Content-Security-Policy
(`docs/SECURITY.md`): its only inline `<script>` is the `application/json` data
island, which is never executed, and it has no inline event handler or `style`
attribute. `OpenBench/tests/test_csp.py` scans the template and the rendered
`/progress/` pages.

Each lineage row carries the dev bench after the date and author, and a second
detail line with the step's speed and cost. The **Economics & speed** section
sits below the lineage: a tile row (speed since the window's start, the share
spent on failed changes, games per Elo at STC and LTC, trunk velocity and the
two latencies), two short charts (speed along the trunk, on the same step axis
as the trunk chart; trunk steps per week), each with its data table, and the
per-class table. It is absent when there is no lineage.

A lineage, candidate, direct-check or detached row that stands for exactly one
workload is a navigable row (`data-row-href` and its one `.row-link`, see
[UI.md](UI.md)); a row pooled from several workloads has no single
destination, so each run keeps its own link. Step subjects and short commit
names come from `OpenBench/listing_rows.py` (`split_info`, `short_name`), the
same as the listings.

The lineage demo is `seed_demo`'s one pinned chain, `COMMIT_CHAIN`: three trunk
steps with STC and LTC stages, a repeated STC to pool, a rejected sibling, a
running LTC confirmation on the newest step and a running candidate off it.
`progress_checks` adds the one run the lab agent does not make, a fixed-games
LTC run of the newest accepted commit against the chain root, which the page
shows as a direct check. The unpinned seeded tests are the detached trees.
Each `DemoCommit` has a `speed` (dev over base nodes per second) that its
Results' node counters follow with a little per-host noise, and the counters'
times follow the time control, so the speed chain and core-hours are
plausible.
