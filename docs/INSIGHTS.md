# Workload insights

The Server keeps a small time series of every Workload's cumulative counters,
and derives progress, throughput, time-left and strength statistics from it.
Two JSON endpoints expose the results for the workload page and the index.
Workloads that upload PGNs also get per-game statistics; see [Games](#games).

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

`diagnosis` is the verdict described under
[Workload diagnosis](#workload-diagnosis):

```jsonc
"diagnosis": {
  "state": "outranked",
  "severity": "warning",
  "headline": "Outranked: workers take the highest priority first, and 1 workload at priority 5 is ahead of this one at priority 0.",
  "brief": "outranked by priority 5",
  "evidence": [
    { "kind": "higher_priority",
      "text": "#12 (Pawn corrhist, LTC) has priority 5, 5 above this workload's 0.",
      "link": { "href": "/test/12/", "label": "Workload 12" } }
  ]
}
```

`evidence[].kind` is one of `workers`, `last_result`, `assignment`,
`last_worker`, `eligible`, `ineligible`, `higher_priority`, `focus`,
`throughput_share`, `error`, `limit`.

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

## Games

Counters say how many pairs fell in each pentanomial bucket; they cannot say
which colour won, how a game ended, how long it lasted or which opening it
came from. When a Workload uploads PGNs, the Server reads those games and
answers these questions on the workload page.

Code lives in `OpenBench/games/`:

| Module | Role |
|---|---|
| `pgn.py` | Tolerant parser for the Client's upload dialect: headers, moves and their comments. |
| `domain.py` | Enums, limits, the three game phases and the adjudication rules of a Workload. |
| `termination.py` | How a game ended: the written reason when the PGN still has one, otherwise inferred. |
| `facts.py` | Everything measured from one game (`GameFacts`). |
| `aggregate.py` | `Aggregate`, the counters over all analysed games, and `PairTracker`. |
| `archive.py` | Streams the members of a `.pgn.tar` from a byte offset within a `Budget`. |
| `service.py` | Persists the aggregate in `GameAnalysis` and resumes it; called by the PGN watcher and the API. |
| `report.py` | Turns an aggregate into the payload (rates, quartiles, top openings). |
| `views.py` | `GET|POST /api/workload/<id>/games/`. |
| `synthetic.py` | Demo and test games in the match runner's format, uploaded through the Client's formatter. |

### Enabling it

Per-game data exists only for Workloads created with **Upload PGNs** set to
`COMPACT` or `VERBOSE`; the setting cannot be changed afterwards. `COMPACT`
gives everything except nodes per second and time per move, which need
`VERBOSE`. A test without uploads shows one muted line under the Insights
heading saying so, and nothing else. A lab agent creating tests through
`/scripts/` sends `upload_pgns=COMPACT`; nothing else is needed, and the
Games group appears on the workload page as soon as the first batch is
archived. [DEPLOYMENT.md](DEPLOYMENT.md#pgn-archive-size) has the disk cost.

### The dialect

`Client/pgn_util.py` rewrites the match runner's PGN before upload. A game is
a block of header lines, a blank line, one line of moves, and a blank line:

```
[Round "1"]
[White "Avalanche-dev"]
[Black "Avalanche-base"]
[Result "1-0"]
[FEN "rnbqkb1r/ppp2ppp/4pn2/3p4/2PP4/2N5/PP2PPPP/R1BQKBNR w KQkq - 2 4"]
[TimeControl "8+0.08"]
[ScaleFactor "1.0"]

Bg5 {+0.31/14} Be7 {-0.25/13} ... Qh7# {+M1/3} 1-0
```

- Headers kept: `Event`, `Site`, `Date`, `Round`, `White`, `Black`, `Result`,
  `FEN`, `TimeControl`, `Variant`, `ScaleFactor`, `SetUp`, and `GameEndTime`
  for `VERBOSE`. The engine names end in `-dev` and `-base`; that suffix is
  how a game's sides are told apart.
- Every move carries one comment. `COMPACT`: `score/depth`. `VERBOSE`:
  `score/depth 0.105s, n=123456, sd=22` (time, nodes, selective depth). Either
  may end in `, line=…`, an engine's `info string pgncomment` text, which is
  ignored. `book` marks a book move and `unknown` a move without a usable
  comment. Scores are in pawns from the mover's side; `+M5` is a mate score.
- **The reason a game ended is not uploaded.** The match runner writes a
  `Termination` header and ends the last comment with a sentence such as
  `Black loses on time`; the Client drops both. Mate is still visible (the
  last move ends in `#`); everything else is inferred, see
  [How games ended](#how-games-ended).

The parser also reads the match runner's own output (move numbers, wrapped
lines, the closing sentence, a `Termination` header), so archives produced by
a Client that keeps the reason would be classified exactly.

A block is **malformed**, counted and skipped, when a header line is not
`[Name "value"]`, `White`, `Black` or `Result` is missing, the result is not
`1-0`, `0-1`, `1/2-1/2` or `*`, the move line does not end in the same result,
a move is not SAN or coordinate notation, the engine names do not end in
`-dev` and `-base`, or the game is longer than 1 MiB. Moves are not checked
for legality. A game with result `*` is **unfinished**: counted on its own and
left out of every statistic.

### What is measured

All of it is from dev's point of view unless it says White.

- **Results by colour**: wins, draws and losses of dev as White and as Black,
  and of the White side whichever engine played it. `score` is
  `(wins + draws / 2) / games`. The White score is the first-move (and book)
  advantage; a gap between dev's two rows that the White score does not
  explain is dev playing one colour better.
- **Pair outcomes**: the two games of an opening are matched within one
  uploaded batch by `Round` and opening, with the colours reversed. Each pair
  is one of `WW`, `WD`, `WL`, `DD`, `DL`, `LL`. `pentanomial` is
  `[LL, DL, DD + WL, WD, WW]`, the same order as the Workload's counters;
  `middle_wl_share = WL / (WL + DD)` is the part of the middle bucket that was
  a win and a loss rather than two draws, which the counters cannot give. A
  `WL` pair means the same colour won both games: `white_sweeps` and
  `black_sweeps` count them by colour. Games whose partner is not in the same
  batch are `unpaired_games`.
- <a id="how-games-ended"></a>**How games ended**: each finished game falls in
  one category. Uploaded PGNs give only these, by inference:

  | Key | Rule |
  |---|---|
  | `checkmate` | Decisive and the last move ends in `#`. |
  | `win_adjudication` | Decisive, no mate, and the loser's last `movecount` own scores were all at or below `-score` of the Workload's Win ADJ. setting. |
  | `unexplained_win` | Any other decisive game: a time loss, an illegal move, a crash or disconnect, or a tablebase adjudication. The Workload's `crashes` and `timelosses` counters say how many of the first three there were. |
  | `draw_adjudication` | Drawn, the game reached `movenumber` and the last `movecount` scores of both sides were within `score` of zero (Draw ADJ.). |
  | `draw_by_rule` | Any other draw: repetition, the fifty-move rule, stalemate, insufficient material or a tablebase adjudication. |

  With adjudication set to `None` the two adjudication rows never appear. When
  a PGN states the reason, it is used as written and may also be `time_loss`,
  `illegal_move`, `disconnect`, `repetition`, `fifty_moves`, `stalemate` or
  `insufficient_material`. `inferred_games` says how many were inferred.
- **Game length**: plies played from the opening position (book moves in the
  PGN count, moves before a `FEN` do not). Mean, quartiles (linear
  interpolation between ranks) and the longest game, for all, decisive and
  drawn games, and a histogram in 20-ply bins. Lengths are exact up to 600
  plies; longer games count as 600.
- **Openings**: the key is the first four fields of the `FEN` header, or the
  book moves when there is no `FEN`. Three lists of at most ten openings:
  `lopsided` ranks by `|2 × dev_score − 1| × pairs` (dev's net pairs won or
  lost there), `colour_bound` by the number of `WL` pairs, `drawn` lists
  openings whose pairs were all `DD` over at least two pairs, most pairs
  first. `always_drawn` counts every such opening. At most 8,192 openings are
  tracked per Workload; pairs from further ones still count everywhere else
  and are reported as `untracked_opening_pairs`.
- **Evaluations** (`evals`, `null` when no game carries a score):
  - `book`: the first reported score of each game seen from White, clamped to
    ±10 pawns, in centipawns: `mean_white_cp` is the book's bias and
    `mean_abs_cp` how unbalanced its positions are.
  - `advantage`: for dev and base at +1.00, +3.00 and +5.00, the games in
    which that engine's own score reached the threshold at least once
    (`reached`), how they ended for it, and `not_won_share`, the share it
    failed to win. A mate score passes every threshold.
  - `phases`: plies 1 to 40, 41 to 80 and 81 onwards, counted from the opening
    position. Per engine: `mean_depth` over moves with a depth, and for
    `VERBOSE` uploads `nps` (nodes over the reported move times) and
    `mean_time_ms`. Times are as reported, not divided by `ScaleFactor`.
    `has_timing` is false for `COMPACT` archives.

### Storage and cost

`GameAnalysis` holds one row per Workload: the aggregate as JSON (`state`),
its schema `version`, `games`, the number of archive `members` read, the byte
offset reached (`analysed_bytes`), and whether the last pass reached the end
of the archive (`complete`). The aggregate is nothing but counters keyed by
name, so two passes over two halves of an archive give the same state as one
pass over the whole. Its size does not depend on the number of games: ply
counts are capped at 600 distinct values and openings at 8,192 rows (about
0.6 MB of JSON at the cap, 5 kB without the opening table).

The archive is append-only, so the offset of the first unread tar header is a
complete cursor. A pass opens the archive there, reads whole members, and
stops when the archive ends or its `Budget` runs out (checked between
members, so a pass always makes progress):

| Caller | Budget |
|---|---|
| PGN watcher, after it appends a Workload's batches (`refresh_after_archiving`) | 4 MiB of compressed batches, 20,000 games or 2 s |
| `/api/workload/<id>/games/`, when the watcher has not caught up | 2 MiB, 10,000 games or 1 s |

A pass that finds nothing new costs one `stat`, one read of a tar header and
one query, and writes nothing. The write is conditional on the row still
being at the offset the pass started from, so two concurrent passes cannot
count a batch twice; the loser discards its work. A row from an older
`version`, or an offset beyond the end of a replaced archive, restarts from
the first byte. The watcher calls the analysis after it has flagged the PGN
rows and deleted the batch files, inside a guard that logs any exception: a
failing analysis never stops archiving.

Limits, each reported under `limits` and named in the page's "Partial" line:

- `complete` is false while a budget stopped a pass before the end; the page
  then asks again every 4 s, and each request advances by one budget.
- A member that is not valid bzip2, is truncated, or expands beyond 64 MiB is
  a `damaged_members` entry; the games read before the damage are kept.
- A member still being appended, or bytes after the cursor that are not a tar
  header, end the pass without moving the cursor, so it is retried.
- Throughput is a few hundred to a few thousand games per second per core in
  pure Python; an archive that predates this feature is caught up over
  successive watcher passes and page views rather than in one request.

### `GET|POST /api/workload/<id>/games/`

Same authentication and errors as the other workload endpoints (`401`
without a view permission, `404` for an unknown id).

```json
{
  "games": {
    "status": "ready",
    "upload_pgns": "VERBOSE",
    "active": true,
    "report": {
      "updated_at": "2026-10-01T05:37:20.114000+00:00",
      "games": 240,
      "limits": {
        "complete": true, "analysed_bytes": 223232, "archive_bytes": 235520, "members": 5,
        "damaged_members": 0, "malformed_games": 0, "unfinished_games": 0,
        "unpaired_games": 0, "untracked_opening_pairs": 0
      },
      "colour": {
        "dev_as_white": { "wins": 34, "draws": 70, "losses": 16, "score": 0.575 },
        "dev_as_black": { "wins": 19, "draws": 70, "losses": 31, "score": 0.45 },
        "white": { "wins": 65, "draws": 140, "losses": 35, "score": 0.5625 }
      },
      "pairs": {
        "total": 120, "ww": 7, "wd": 21, "wl": 18, "dd": 49, "dl": 21, "ll": 4,
        "pentanomial": [4, 21, 67, 21, 7], "middle_wl_share": 0.2687,
        "white_sweeps": 14, "black_sweeps": 4
      },
      "terminations": {
        "inferred_games": 240,
        "rows": [ { "key": "draw_adjudication", "label": "Draw adjudication", "games": 78, "share": 0.325 } ]
      },
      "lengths": {
        "all": { "games": 240, "mean": 135.2, "q1": 108.0, "median": 127.0, "q3": 163.0, "longest": 355 },
        "decisive": { "games": 100, "mean": 120.5, "q1": 97.0, "median": 120.0, "q3": 144.0, "longest": 219 },
        "drawn": { "games": 140, "mean": 145.8, "q1": 111.0, "median": 134.0, "q3": 173.0, "longest": 355 },
        "bin_plies": 20,
        "histogram": [ { "first_ply": 1, "last_ply": 20, "decisive": 0, "drawn": 0 } ]
      },
      "openings": {
        "tracked": 16, "always_drawn": 1,
        "lopsided": [ {
          "opening": "rnbqkb1r/ppp2ppp/4pn2/3p4/2PP4/2N5/PP2PPPP/R1BQKBNR w KQkq -",
          "pairs": 15, "ww": 0, "wd": 4, "wl": 0, "dd": 11, "dl": 0, "ll": 0,
          "white_sweeps": 0, "black_sweeps": 0, "dev_score": 0.567
        } ],
        "colour_bound": [],
        "drawn": []
      },
      "evals": {
        "games": 240,
        "book": { "games": 240, "mean_white_cp": 26.1, "mean_abs_cp": 35.2 },
        "advantage": [ {
          "side": "dev", "threshold_cp": 100, "reached": 66, "won": 52, "drawn": 11, "lost": 3,
          "not_won_share": 0.212
        } ],
        "phases": [ {
          "key": "early", "label": "Plies 1 to 40",
          "dev": { "moves": 2400, "mean_depth": 17.7, "timed_moves": 2400, "nps": 1213000.0, "mean_time_ms": 312.0 },
          "base": { "moves": 2400, "mean_depth": 17.7, "timed_moves": 2400, "nps": 1182000.0, "mean_time_ms": 313.0 }
        } ],
        "has_timing": true
      }
    }
  }
}
```

(Lists trimmed to their first entry. `openings` rows have the same fields in
all three lists; `advantage` has six rows, `phases` three.)

- `status`: `disabled` when the Workload has `upload_pgns` `FALSE` (no file
  or analysis is looked at), `empty` when no batch has been archived yet,
  `ready` otherwise. `report` is `null` unless `ready`.
- `active`: the Workload is not finished, so more batches may arrive; the
  page then asks again every minute.
- A ratio is `null` when its denominator is zero.

Cost: 4 queries when disabled, 5 when the analysis is up to date (the
Workload, then the `GameAnalysis` row), plus one conditional update when the
request had to analyse.

### On the page

`OpenBench/static/games.js`, loaded only when the Workload uploads PGNs,
fills a `data-games-insights` container at the end of the Insights section
and leaves it hidden until a report with games arrives:

- Tiles: games analysed (pairs and batches), White score with dev's score per
  colour, split pairs (`middle_wl_share`), median length, and book balance
  when games carry scores. A "Partial" line lists whatever `limits` reports.
- Tables: results by colour with a win/draw/loss bar and its legend, pair
  outcomes with the pentanomial bucket each belongs to, and how games ended,
  with a note when endings are inferred.
- A stacked histogram of game length (decisive in `--series-1`, drawn in
  `--series-2`, with a legend), described by its `aria-label` and by the
  quartile table beside it, which `aria-details` names.
- The three opening tables, then unconverted advantages and search by phase
  when `evals` is present. NPS and time columns appear only with timing.

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
  - A Games group when the Workload uploads PGNs, or one line saying how to
    get it when a test does not; see [Games](#games).
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

## Workload diagnosis

A workload can sit approved with no games, or stop part-way, with nothing on
the page saying why. `OpenBench/diagnosis/` answers "what is this workload
waiting for" as one structured verdict, built from the scheduler's own rules
and from what the server has recorded. It never guesses: a verdict is a fixed
template filled from evidence, and `unknown` when the evidence is not there.

### The verdict

| Field | Meaning |
|---|---|
| `state` | One of the states below |
| `severity` | `ok` (running, finished), `info` (pending, starting, queued, unknown) or `warning` (blocked or stalled) |
| `headline` | One sentence, for the workload page's banner |
| `brief` | A few words, for a listing row |
| `evidence` | `[{ "kind", "text", "link": { "href", "label" } \| null }]`, each a fact the verdict rests on |

States, in the order they are decided:

| State | When |
|---|---|
| `finished` | Finished, stopped or deleted: no worker will take it. |
| `stopped_by_error` | Stopped, and the workload's newest logged event is a worker error without a log file: `clientBenchError` finishes a test on a bench mismatch and logs exactly that. A later Stop, Restart or other action logs its own event and takes this state away. |
| `awaiting_approval` | Not approved yet. |
| `running` | A machine seen in the last 2 minutes holds it, and has not reported an error for it since it last checked in (a Client that fails to build reports once and drops the workload). Evidence: the machines, and when the last result arrived. |
| `stalled` | Such machines still check in, but no result arrived within the stall limit of the last one, or within the build allowance plus the stall limit of the workload being taken. |
| `starting` | No holder is checking in, but a machine took it within the build allowance (30 minutes) and has reported no game and no error. A worker is silent while it builds and benchmarks. |
| `failing` | Nobody holds it and workers have reported build failures since its last result: a commit that does not build fails on every worker, so this is said whatever the fleet looks like now. |
| `no_workers` | No machine has been seen in the last 10 minutes, and the fleet of the last 24 hours includes one that could take it (or there is no such fleet). Says when the last worker was seen. |
| `no_eligible_workers` | Workers were seen, but none that could take it; each kind is listed with every rule that excludes it. Also when the only kinds that could take it have gone quiet while others are online. |
| `outranked` | An eligible worker is online, but for every such worker a higher-priority workload it can play comes first, or its `--focus` / `--only` engine has work waiting. Evidence: those workloads and the priority gap. |
| `low_share` | It is among the candidates, but other candidates have fewer threads for their throughput, so they are served first. |
| `failing` | Also: it is next in line for an online worker, yet workers have reported other errors for it since its last result. Evidence: the last three, linked to their logs. |
| `waiting` | It is next in line for an online worker and nothing is wrong: a busy worker asks again only when its batch ends. |
| `unknown` | The workload or every registration seen cannot be evaluated (a missing `Threads` option, a registration without the fields the scheduler reads), or the queue cannot be ranked because an active workload has a throughput of 0. |

Worker errors newer than the last result are attached to every active state,
so a `no_workers` verdict still shows that the last workers failed.

### Same rules as the scheduler

`clientGetWorkload` decides in `OpenBench/workloads/get_workload.py`. The
diagnosis calls the same functions rather than restating them, through the
typed wrappers in `diagnosis/scheduler.py`:

- `unmet_syzygy_requirements`, `valid_hardware_assignment`,
  `workload_uses_time_based_tc` and the Machine's stored `supported` and `only`
  lists decide whether a machine may play a workload
  (`diagnosis/eligibility.py`). Every failing rule is reported, in the
  scheduler's order: engine unsupported, `--only`, blacklist, Syzygy, `--noisy`,
  threads.
- `refine_candidates` keeps the highest priority among what the machine may
  play and then applies its focus; `distribute_resources` and
  `apply_resource_ratios` give each candidate its threads-per-throughput ratio,
  and the lowest ratio is served next (`diagnosis/standing.py`).

Three of those (`unmet_syzygy_requirements`, `refine_candidates`,
`distribute_resources` with `apply_resource_ratios`) were extracted from
`filter_valid_workloads`, `select_workload` and
`compute_resource_distribution` without changing them, so the diagnosis can
run them on machines it already loaded. An unsupported engine is explained by
`diagnosis/engine_support.py`, which is also what `views.supported_engines`
now calls at registration: a missing CPU flag, compiler, Git token or
operating system.

`OpenBench/tests/test_diagnosis.py` holds the agreement tests: a machine
registers through `clientWorkerInfo`, the diagnosis names the workloads it is
next in line for, and `clientGetWorkload` must hand out one of exactly those,
or nothing when there are none. They cover every exclusion, priority, focus,
throughput share, engine balancing and the blacklist after a build failure.

### The fleet

Live workers here are short-lived batch jobs that register a new Machine each
time, so "the fleet" cannot be the list of Machines. `diagnosis/fleet.py`
takes the 120 most recently seen Machines, keeps those seen in the last 24
hours, and groups them by owner, pool (`fleet.pools.pool_label`, so
`batch-<uuid>:0` jobs read `batch-*`), CPU and the registration fields the
scheduler reads (threads, cores, supported engines, `--only`, `--focus`,
Syzygy, `--noisy`, operating system). Each group is judged once, through its
newest Machine, and reported as one line with the number of hosts
(`Machine.host_key`) behind it and its last sighting. A group is "online" when
its newest Machine was seen in the last 10 minutes.

Registration housekeeping deletes an owner's exited `--single_workload` and
`--fleet` registrations that never took a workload once they are 15 minutes
old. The 24-hour fleet is therefore the Machines that played something plus
whatever registered recently: a kind of worker that only ever registered and
left is forgotten after a quarter of an hour.

### Limits

- **Idle workers are invisible.** `Machine.updated` moves when a worker
  registers, takes a workload, reports or sends a heartbeat. A worker that
  polls and is given nothing leaves no trace, so "last seen" can be older than
  its last request, and `no_workers` means no recorded contact, not no polling.
- **The blacklist is inferred.** A Client sends its blacklist with each
  workload request and the server does not store it. The Client adds a
  workload after a build failure, which it also reports, so a Machine with a
  `... build failed` error for the workload is treated as refusing it for that
  session. That only applies to Clients that keep polling: a `--single_workload`
  or `--fleet` Client exits, and the job that replaces it starts with an empty
  blacklist and is handed the workload again, so such registrations are judged
  as a fresh one would be and stay in their pool's group. A `--blacklist`
  given on the command line cannot be seen.
- **Next in line is not a promise.** The scheduler picks at random among equal
  ratios and lets a machine keep its workload while the split stays within 25%
  of fair, so `waiting` means the next free worker would be offered it or a
  tied workload.
- **Build allowance.** Thirty minutes from taking a workload, during which
  silence reads as `starting` and no first result is due. A build that takes
  longer reads as a worker that left.
- **Stall limit.** Ten minutes, or four times an estimated game
  (`2 × (base + 80 × increment)` of the dev time control) when that is longer.
  Tunes that report in bulk only report at the end and are never called
  stalled.
- **Sampling.** Only the 120 newest Machines and the 200 newest worker errors
  of the active workloads are read. With more than 120 Machines online the
  thread counts behind `low_share` undercount.

### Where it shows, and what it costs

- **Workload page**: a banner above the configuration for every workload that
  is not finished, and for one stopped by a worker error
  (`Blocks/diagnosis.html`): quiet with collapsed evidence for
  `ok` and `info`, amber with the evidence open for `warning`.
- **Listings**: an active row whose timing line has no rate shows `brief`
  under it, amber for a warning, with the headline as its tooltip. Rows that
  are producing games are left alone. A finished row stopped by a worker
  error shows that error the same way.
- **API**: `insights.diagnosis` on `/api/workload/<id>/insights/`, and the
  state and headline on each row of `/api/workloads/`
  ([API.md](API.md#getpost-apiworkloads)).

Every active workload is diagnosed from one shared picture of the fleet, in
six queries whatever the number of workloads and Machines: the active
workloads, their worker errors, the newest Machines, the engine
configurations, the last result time per workload, and the Results still
without a game. A pending or finished workload costs none; stopped ones cost
one query between them, for their newest events. A listing pays the six only
when an active row has no rate, and the one only when it lists a stopped
workload. Both error lookups use the `logevent_test_machine` index.

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

`seed_demo` also gives one active test (`VERBOSE`) and one finished test
(`COMPACT`) a PGN archive of 240 games each, written in the match runner's
format by `OpenBench/games/synthetic.py` and passed through the Client's own
`pgn_util.compress_pgn_files`, so the archives are in the real upload
dialect. The moves are placeholders, not legal games; results, scores,
lengths and endings are drawn per opening so every table has something to
show.

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

The page has two halves. The **lineage** answers "how much stronger did the
engine get, and through which commits"; the **activity** charts answer "how
much testing happened".

Code lives in `OpenBench/progress/`:

| Module | Role |
|---|---|
| `domain.py` | `Window`, `TimeClass`, the source rows and the frozen report dataclasses: the contracts everything else is written against. |
| `conditions.py` | Classifies a test's time control and thread count into a `TimeClass`. |
| `lineage.py` | Pure graph functions: steps, pooling, the trunk, branches, chained series, direct checks, the window. |
| `analysis.py` | Pure functions for the activity half: parsing, the window scope, games per day, weekly series, rankings, the summary. |
| `sources.py` | The queries below. |
| `report.py` | Runs the queries and assembles a `ProgressReport`. |
| `present.py` | Formats the report for the template (tiles, table rows, links). |
| `views.py` | The page and the JSON endpoint. |

`OpenBench/tests/test_progress_lineage.py` covers the graph functions on
hand-built graphs without a database; `test_progress.py` covers the activity
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

### Cost

Six queries whatever the data size (five for `all`, which needs no baseline),
each an aggregate or a bounded row set: the runs (one row per SPRT or GAMES
test of the engine, joined to its two `Engine` rows for the SHAs, with a
correlated newest-snapshot subquery served by the `(test, created)` index), the
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
                    "elo": { "lower": 1.06, "value": 3.70, "upper": 6.34 } }
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
fields, which nothing else consumed.

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
