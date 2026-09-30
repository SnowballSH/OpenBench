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
| `grouping.py`, `contributions.py` | Per-machine and per-CPU contribution; `grouping.sum_by_key` also backs `fetch_result_summaries`. |
| `sources.py` | The only module that reads Workload models; turns rows into domain values. |
| `workload.py`, `server.py` | Assemble the two payloads. |
| `serialize.py`, `api.py`, `views.py` | JSON conversion and the HTTP endpoints. |

Everything except `sources.py`, `recorder.py`, the loaders in `server.py`, and
the views is a pure function of domain values, and is unit tested without a
database (`OpenBench/tests/test_insights_analytics.py`).

## The `WorkloadSnapshot` model

| Field | Meaning |
|---|---|
| `test` | `ForeignKey(Test, CASCADE, related_name='snapshots')` |
| `created` | When the snapshot was taken; indexed on its own and together with `test`. |
| `games`, `losses`, `draws`, `wins` | Cumulative trinomial counters, copied from the Test. |
| `LL`, `LD`, `DD`, `DW`, `WW` | Cumulative pentanomial counters, copied from the Test. |
| `llr` | `Test.currentllr` at that moment; `0.0` outside SPRT. |

Deletion: `Result` protects its `Test` (`PROTECT`) because Results are source
data. Snapshots are derived entirely from the Test's own counters, so they
`CASCADE`: they never block deleting a Test and never outlive one. Workloads
are normally only flagged `deleted`, which leaves the history in place.

## Recording

`OpenBench.utils.update_test` calls `record_snapshot(test)` once, at the end of
its `transaction.atomic()` block, after the Test row is saved and while it is
still locked by `select_for_update()`. The snapshot therefore always agrees with
the Test counters it copies.

A snapshot is written when any of these hold:

- the Workload has no snapshot yet (the first report is always recorded);
- the newest snapshot is at least `SNAPSHOT_INTERVAL` (60 s) old;
- the report just finished the Workload (the finishing point is always kept).

Cost on the hot path: one aggregate query over the `(test, created)` index,
returning the newest `created` and the row count. At most once a minute per
Workload, one `INSERT` follows. When that insert would take the history past
`SNAPSHOT_LIMIT` (400 rows), the history is thinned in the same transaction:
one indexed read of `(id, created)` and one `DELETE ... WHERE id IN (...)`,
which happens about once every 200 inserts (about every 3.3 hours of play).

Thinning (`thinned_ids`) splits the span between the first and last snapshot
into `SNAPSHOT_TARGET` (200) equal time buckets, keeps the newest snapshot in
each bucket, and always keeps the first and last snapshot. The history is left
roughly uniform in time with at most 201 rows, then grows at one row per minute
until the next thinning, so a Workload never holds more than 400 rows. Recent
activity is always at one-minute resolution; old activity coarsens to
`span / 200`.

Manual stops (`modify_workload`) do not pass through `update_test`, so a stopped
Workload's newest snapshot may be up to a minute behind its counters. The
insights payload covers that gap with the live tail point described below.

## Timeline used by the analytics

- **Snapshots exist:** the snapshots in time order. If the Test's counters are
  ahead of the newest snapshot (reports throttled within the last minute), a
  tail point is appended from the Test counters at `Test.updated`.
- **No snapshots, games > 0** (Workloads that ran before this feature): a single
  synthetic point from the Test counters at `Test.updated`, and
  `history.synthetic` is `true`. Rates treat the Workload as having played
  evenly from `Test.creation` to `Test.updated`.
- **No snapshots, no games:** an empty history; timing starts at creation.

`started_at` is the first snapshot's time (or `Test.creation` without history).
The first snapshot is taken after the first batch is reported, so the first
batch's own duration is not counted. `ended_at` is the last timeline point for a
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
Brownian approximation ignores the overshoot of the final step, so it slightly
overestimates; and the empirical `p̂` changes as the test runs, so the estimate
moves with it. The probability `P` is not reported: it is conditional on the
current Elo estimate being the truth, and reads as far more confident than the
LOS for the same data.

### Contributions

From the Workload's `Result` rows, grouped by Machine and by `cpu_name`
(missing names become `Unknown`):

- `games`, `pairs` summed; `share = games / all games of the Workload`;
- `pairs_per_hour = 3600 · pairs / timing.elapsed_seconds`: the average rate
  over the Workload's whole elapsed time, not over the time that machine was
  actually assigned, since Results carry no start time;
- `elo` from the group's own summed counters, as above.

### Server

- `fleet`: Machines updated within the last 2 minutes
  (`utils.getRecentMachines`), their summed `concurrency` as threads, and
  `Σ concurrency · mnps`, matching the index page's status line.
- `workloads`: counts from `utils.get_pending_tests` and `get_active_tests`.
- `games_last_24h`: for every Workload with a snapshot in the window, its
  current `Test.games` minus the games at the window start. The start value is
  interpolated between the newest snapshot before the window and the oldest one
  inside it, which keeps thinned histories accurate; a Workload with no earlier
  snapshot counts from zero. Reports within a minute of a Workload's last
  snapshot before the window can be missed.
- `finished_last_7d`: finished, non-deleted Workloads whose `Test.updated` is in
  the window. `Test.updated` also moves on later edits, so this is "finished
  and touched within 7 days". `stopped` counts finished Workloads that neither
  passed nor failed. `sprt_pass_rate = sprt_passed / (sprt_passed + sprt_failed)`,
  `null` without a decided SPRT.
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
queries: status 200 with `{ "error": "..." }`.

```jsonc
{
  "insights": {
    "generated_at": "2026-09-30T04:14:46.613352+00:00",
    "workload": {
      "id": 1,
      "mode": "SPRT",                 // SPRT | GAMES | SPSA | DATAGEN
      "status": "active",             // pending | active | passed | failed | stopped | deleted
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
      "completes_at": "..."           // or null
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
      "window_days": 7, "total": 3, "passed": 2, "failed": 1, "stopped": 0,
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

## Demo data

`seed_demo` gives every seeded Workload with games a 150-point history between
its start and its finish (or now), with jittered arrival rates and LLRs
computed at every point, ending exactly at the Workload's counters. It also
credits each Profile with the games its Machines played, so the server
payload's `top_contributors` is populated.
