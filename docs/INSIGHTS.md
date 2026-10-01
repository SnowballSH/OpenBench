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
| `speed.py` | Nodes per second from node and millisecond counters, shared by the result summaries and the machine page; the `SpeedCounters` a Result carries. |
| `results/` | The Results analysis, one module per part: `breakdown.py` (pair outcomes and pair variance), `outlook.py` (SPRT forecast), `speeds.py` (dev against base search speed), `hosts.py` and `consistency.py` (CPU and host agreement, crashes, time losses), `verdict.py` (the plain-language line), `analysis.py` (assembly). See [Results](#results). |
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

### Results

`results` reads the same Test counters and the same `Result` rows as the rest
of the payload (the rows gain the crash, time-loss and node/time columns; the
query count does not change). It is `null` for SPSA. PGNs are not involved:
the live instance runs with `upload_pgns` off, so everything here comes from
the cumulative counters.

#### Outcome breakdown

- `pentanomial_fractions` and `trinomial_fractions`: each bucket over the pair
  (or game) count, in the order `[LL, LD, DD, DW, WW]` and `[L, D, W]`.
- `decisive_game_rate = (W + L) / games`, and `games_per_decisive` its inverse.
- `swept_pair_rate = (LL + WW) / pairs`, the pairs one side won twice;
  `level_pair_rate = DD / pairs`. The middle bucket holds two draws and a win
  with a loss alike. The counters cannot tell those apart, so nothing here
  pretends to.
- `pair_variance` compares the observed variance of a pair's mean score
  (`observed`, on a 0 to 1 scale) with the variance the mean of two
  independent games would have, `independent = σ²_game / 2`, where `σ²_game`
  is the trinomial per-game variance pooled over both colours.
  `ratio = observed / independent`. `game_correlation = ratio − 1` is the
  correlation `ρ` between the two games of a pair under the assumption that
  both have that same variance, since then
  `Var(pair mean) = σ²_game (1 + ρ) / 2`. `pair_efficiency = 1 / ratio` is
  the number of pairs of independent games that estimate the mean score as
  precisely as one paired-opening pair does (so one such pair is worth
  `2 / ratio` independent games). Playing each opening with both colours
  makes the two games negatively correlated when openings are biased (one
  side wins both ways round, the pair is level), so real tests show a ratio
  below 1. It is `null` when either variance is zero or when
  `games ≠ 2 · pairs`.

#### SPRT outlook

`outlook` is a forecast for an unfinished SPRT: `pass_probability`, and the
median and 80% range of the games still needed. It is `null` for other modes,
for finished tests, wherever the time-left estimate is unavailable (fewer
than 200 games, LLR outside the bounds, no variance, an empty trinomial
bucket), and until the games pin the strength down: the standard error of
the normalized Elo, `(800 / ln 10) / √(2N)` over `N` pairs, must be at most
twice the bound width `elo1 − elo0`. With `[0, 3]` that is 1,676 pairs.
Below about 1,000 pairs at `[0, 3]` the forecast was no better than quoting
the base rate when patches are worth about nothing (log loss 0.60 and 0.59
against 0.57 with the chosen prior), so it is not shown, and the tables below
start where it is.

Two earlier attempts failed and explain the design. The plug-in probability
from [the SPRT estimate](#the-sprt-estimate) treats the current Elo estimate
as the truth, and read 0.999 next to an LOS of 0.90. Averaging over a flat
prior instead is honest only when tests are decided by effects far larger
than the bounds; at the live bounds `[0, 3]`, where patches are worth a few
Elo, it read "98% to pass, 2.1k more games" after 200 games of a test whose
Elo was +30 ± 28, and such forecasts passed about two times in three.

1. **Evidence.** The LLR is modelled as Brownian motion with drift `μ` per
   step and the per-step variance `σ²` of `sprt.llr_increment`. The drift is
   linear in the true strength: `σ` per unit of t-value, where the t-value is
   `(mean score − 0.5) / score sd` and one unit is `(800 / ln 10) / √2`
   normalized Elo for pairs (`800 / ln 10` per game for a trinomial test).
   `N` steps estimate the drift as `LLR / N` with variance `σ² / N`.
2. **Prior.** The true normalized Elo is `Normal(0, τ²)` with
   `τ = max(4, 4/3 · (elo1 − elo0))`: a patch is worth about nothing, give or
   take roughly the difference the test was built to resolve. In drift terms
   that is `n₀ = (elo per t-unit / τ)²` earlier steps that averaged exactly
   zero Elo (3,772 pairs for `τ = 4`), so the posterior is
   `μ ~ Normal((N · LLR/N + n₀ · μ₀) / (N + n₀), σ² / (N + n₀))`, with
   `μ₀ = LLR/N − σ · t̂` the drift a zero-Elo patch would have. The payload's
   `prior` reports `mean_elo` (0), `sd_elo` (`τ`) and `equivalent_games`.
3. **Chance to pass.** For each of 61 drifts across ± 6 posterior standard
   deviations, `sprt.expected_exit` gives the probability of reaching the upper
   bound first from the current LLR; the weighted mean is `pass_probability`.
4. **Games to decide.** For each drift, the survival function of the exit
   time between two absorbing bounds a distance `L` apart is computed by the
   method of images (the walk and its reflections in both bounds, two each
   side, each weighted `exp(μ·shift/σ²)`) while the walk has spread less than
   `L / 2`, and for any drift with `|μ| L / σ² > 8`; and by the eigenfunction
   series `Σₖ cₖ · exp(−(μ²/2σ² + k²π²σ²/2L²) n)` (24 terms) otherwise. The
   images converge when the series cancels badly and the reverse, and both
   keep the bound behind a strong drift. The posterior-weighted mixture is
   inverted by bisection for its 10%, 50% and 90% points
   (`remaining_games.lower`, `.median`, `.upper`, in games; `interval` is
   `0.8`).

**Choosing `τ`.** No prior is calibrated for every population of patches, so
`τ` was chosen against two: a narrow one, true normalized Elo `Normal(0, 2²)`,
and a wide one, uniform from 6 below the lower bound to 6 above the upper.
2,500 real pentanomial SPRTs (`OpenBench.stats.PentanomialSPRT`, reports of
16 to 32 pairs) were played for each, and every forecast scored by log loss,
at `[0, 3]`, summed over the 1k to 3k and 3k to 10k pair buckets and both
populations:

| Prior | flat | `τ = 3` | `τ = 4` | `τ = 5` | `τ = 8` |
|---|---|---|---|---|---|
| Summed log loss | 2.215 | 2.011 | 2.000 | 2.017 | 2.082 |

The flat prior is far worse than any of them; between 3 and 5 the total
hardly moves, and what changes is which population pays.
`τ = 3` is the better prior for the narrow population and under-confident for
the wide one (forecasts near 0.5 passed 0.70); `τ = 5` and above the reverse
(forecasts near 0.7 passed 0.45 under the narrow one). `τ = 4` splits the
error. The scaling with the bound width assumes that people choose bounds to
match the effects they expect; the non-regression rows below check it at a
width of 4.

**Calibration at the chosen prior.** Each cell is mean forecast → share that
passed (forecasts), for forecasts below 0.1, 0.1 to 0.4, 0.4 to 0.6, 0.6 to
0.9 and above 0.9. "Constant" is the log loss of always quoting the bucket's
pass rate. "Actual / median" is the median ratio of the games actually needed
to the forecast median, and "inside" the share inside the 80% range.

Bounds `[0, 3]`, narrow population (25% pass):

| Pairs | Log loss (constant) | < 0.1 | 0.1–0.4 | 0.4–0.6 | 0.6–0.9 | > 0.9 | Actual / median | Inside |
|---|---|---|---|---|---|---|---|---|
| 1.7k–3k | 0.553 (0.567) | 0.06→0.07 (303) | 0.25→0.19 (1249) | 0.49→0.33 (605) | 0.71→0.50 (332) | 0.95→0.82 (11) | 1.42 | 0.80 |
| 3k–10k | 0.484 (0.573) | 0.04→0.04 (1152) | 0.24→0.18 (1865) | 0.49→0.36 (855) | 0.73→0.54 (807) | 0.95→0.85 (150) | 1.34 | 0.80 |
| 10k+ | 0.470 (0.636) | 0.05→0.05 (621) | 0.23→0.18 (1006) | 0.49→0.41 (514) | 0.74→0.64 (634) | 0.95→0.94 (175) | 1.14 | 0.80 |

Bounds `[0, 3]`, wide population (48% pass):

| Pairs | Log loss (constant) | < 0.1 | 0.1–0.4 | 0.4–0.6 | 0.6–0.9 | > 0.9 | Actual / median | Inside |
|---|---|---|---|---|---|---|---|---|
| 1.7k–3k | 0.515 (0.692) | 0.06→0.06 (312) | 0.24→0.30 (1018) | 0.49→0.61 (535) | 0.73→0.85 (571) | 0.93→0.98 (64) | 0.85 | 0.82 |
| 3k–10k | 0.391 (0.692) | 0.04→0.03 (1066) | 0.24→0.29 (1272) | 0.50→0.57 (588) | 0.76→0.86 (988) | 0.96→0.98 (577) | 0.89 | 0.80 |
| 10k+ | 0.450 (0.693) | 0.05→0.04 (291) | 0.24→0.29 (456) | 0.50→0.57 (263) | 0.76→0.78 (407) | 0.95→0.99 (185) | 0.96 | 0.80 |

Non-regression bounds `[−3, 1]` (`τ = 5.33`), narrow population (67% pass):

| Pairs | Log loss (constant) | < 0.1 | 0.1–0.4 | 0.4–0.6 | 0.6–0.9 | > 0.9 | Actual / median | Inside |
|---|---|---|---|---|---|---|---|---|
| 1k–3k | 0.639 (0.636) | 0.07→0.36 (78) | 0.28→0.48 (1095) | 0.51→0.65 (1274) | 0.75→0.74 (2119) | 0.94→0.87 (434) | 1.75 | 0.77 |
| 3k–10k | 0.533 (0.648) | 0.05→0.14 (302) | 0.25→0.38 (979) | 0.51→0.63 (825) | 0.76→0.77 (1548) | 0.95→0.94 (875) | 1.47 | 0.78 |
| 10k+ | 0.529 (0.678) | 0.06→0.08 (123) | 0.25→0.33 (526) | 0.50→0.57 (412) | 0.76→0.76 (708) | 0.95→0.96 (234) | 1.19 | 0.79 |

Non-regression bounds `[−3, 1]`, wide population (49% pass):

| Pairs | Log loss (constant) | < 0.1 | 0.1–0.4 | 0.4–0.6 | 0.6–0.9 | > 0.9 | Actual / median | Inside |
|---|---|---|---|---|---|---|---|---|
| 1k–3k | 0.536 (0.693) | 0.05→0.03 (274) | 0.26→0.21 (1387) | 0.50→0.44 (1184) | 0.75→0.71 (1727) | 0.95→0.95 (423) | 0.99 | 0.84 |
| 3k–10k | 0.393 (0.693) | 0.04→0.03 (633) | 0.24→0.20 (981) | 0.50→0.45 (507) | 0.77→0.77 (929) | 0.96→0.96 (754) | 0.96 | 0.83 |
| 10k+ | 0.476 (0.693) | 0.05→0.04 (85) | 0.25→0.21 (265) | 0.50→0.48 (164) | 0.76→0.74 (301) | 0.95→0.95 (107) | 0.99 | 0.81 |

For comparison the flat prior at `[0, 3]`, narrow population, 1k to 3k pairs:
log loss 0.726, forecasts averaging 0.95 passed 0.52, actual / median 2.92.

What the tables say, plainly:

- The forecast is a compromise. When patches really are worth about nothing
  it is still too optimistic in the middle (a 0.7 passes about half the
  time) and tests take about 1.4 times the forecast median early on; when
  effects are large it is too cautious by about 0.1. Forecasts below 0.1 and,
  from 10k pairs, above 0.9 are reliable under both.
- At `[−3, 1]` with patches worth about nothing, most tests pass, and the
  forecast is too pessimistic at the low end and no better than the pass rate
  until about 3k pairs.
- Workers report in batches and the LLR is checked per report, so a test runs
  a little past its bound and lasts a few percent longer than a
  continuous-time model says.
- It assumes the test keeps drawing from one distribution: no worker joining
  with a different speed, no change of book.

`OutlookCalibrationTests` replays a small version of the first two tables in
the suite (real SPRTs at `[0, 3]`, both populations, forecasts at 1.7k, 4k
and 10k pairs) and fails if the forecast is not better than the flat prior
early on. `ExitTimeLawTests` checks the exit-time law against simulated walks
on both sides of the series/image switch, including a strong drift starting
next to the bound behind it.

#### Search speed

Workers report, per Result, the nodes searched and the time used by dev and
base (`dev_nodes`, `dev_time`, …), once at the end of each stint on a
Workload, so the counters cover completed stints only. `speed` is `null` until
one arrives.

- `speed.dev_nps`, `speed.base_nps`: `1000 · nodes / time`, pooled over the
  group; `speed.difference = dev_nps / base_nps − 1` is that pooled ratio, in
  which a host counts in proportion to its playing time.
- `speed.dev_nps_scaled`, `speed.base_nps_scaled` use `dev_time_scaled` /
  `base_time_scaled`. The Client divides each move time by the machine's scale
  factor (`scale_nps / benchmarked nps`), so these are nodes per second of
  reference-machine time and can be compared across machines. Dev and base
  share one factor within a Result, so the difference is the same either way.
- `spread` asks whether the difference is beyond the noise across hosts: the
  95% Student-t interval of the mean of `ln(dev_nps / base_nps)` over the
  hosts that reported counters (`hosts`), every host counting once, given
  back as relative differences (`mean`, `lower`, `upper`). `beyond_noise` is
  true when the interval excludes zero; hosts that agree exactly (no spread at
  all) prove nothing and are never beyond noise. It needs two hosts, and is
  `null` with one.
- `difference` on the reading is the headline, and is the estimate the test
  is about: `spread.mean` when there is a spread, else the pooled ratio. The
  pooled and per-host figures can differ in sign when one long-running host
  dominates the playing time.
- `overall` is the whole Workload; `cpus` repeats the reading per CPU model,
  largest first.

#### Consistency

A host is `ResultRow.host`, the `Machine.host_key` of
[the fleet pages](#fleet-pages): one physical host registers many `Machine`
rows, and an AWS Batch job is a host of its own that plays one Workload for a
few hundred pairs and never returns. A Workload therefore has many small
hosts, so the CPU model is the primary grouping and hosts are only screened
for outliers. A flagged host carries its `machine_label` (long ids
shortened) and its `pool` label.

- **Per CPU** (`cpus`, largest first): games, pairs, Elo, crashes, time losses
  and speed, and `deviation`: the z-score of the group's mean score per pair
  (per game for trinomial) against every other result of the Workload,
  `z = (m − m_rest) / √(s² (1/n + 1/n_rest))` with the Workload's pooled score
  variance `s²`. It is `null` unless both the group and the rest hold at least
  `min_samples` (200) pairs, where the normal approximation of a five-valued
  score is safe. `p_value` is two-sided; `adjusted_p_value` is Bonferroni,
  `min(1, p · groups tested)`; `flagged` is `adjusted_p_value < alpha`.
- **`heterogeneity`**: a chi-square test that every CPU shares one mean score,
  `Q = Σ nᵢ (mᵢ − m)² / s²` with `groups − 1` degrees of freedom. CPUs below
  `min_samples` are pooled into one group (`pooled_small_groups` says how
  many) and dropped if even their pool is too small. `null` with fewer than
  two groups. `flagged` is `p_value < alpha`.
- **Hosts** (`hosts`): `total` hosts, `tested` of them large enough for the
  same z-test against the rest (Bonferroni across the hosts tested), and
  `flagged`, the hosts that deviate, crash or lose on time too often, largest
  first. Statistics are built only for the flagged ones.
- **False flags.** `alpha` is 0.001 per family, and there are three families
  (CPUs, the chi-square, hosts), each looked at again on every refresh. In a
  simulation of honest Workloads (four new hosts of 100 to 600 pairs per
  look, two CPU models, 30 looks) some deviation flag was up on 0.4% of looks
  and on 1.5% of Workloads at some point in their life. With `alpha = 0.01`
  those were 1.5% and 8.8%, which is why it is not 0.01.
- **Crashes and time losses**: `crash_rate` and `timeloss_rate` are per game.
  `crash_flagged` needs at least 2 crashes and a rate above 1 in 1,000;
  `timeloss_flagged` at least 2 time losses and a rate above 1 in 200. A
  single crash never earns a flag: on a host with a few hundred games it says
  nothing about a rate.

A flag is a reason to look, not a verdict. A patch can genuinely be worth more
on one CPU (a speed-up that depends on the instruction set), and a deviating
host can be a broken build, a throttled machine or chance.

#### Verdict

`verdict.text` is one or two sentences built from fixed templates
(`verdict.py`), no model involved; `kind` and `tone` classify it.

| `kind` | When | `tone` |
|---|---|---|
| `too_early` | fewer than 200 games, or every result identical (no variance) | `neutral` |
| `passed`, `failed` | an SPRT that passed or failed | `positive`, `negative` |
| `very_likely_gain` / `_loss` | LOS prints as ≥ 98% / ≤ 2% | `positive` / `negative` |
| `likely_gain` / `_loss` | LOS prints as ≥ 90% / ≤ 10% | `positive` / `negative` |
| `possible_gain` / `_loss` | LOS prints as ≥ 70% / ≤ 30% | `neutral` |
| `inconclusive` | anything between | `neutral` |

The class is decided on the rounded percentage that is printed, so a headline
never contradicts the number beside it. The headline's confidence is about
the sign only. A size word is added only when the whole 95% interval is on
one side of zero, and it describes the end nearest zero: small below 5 Elo,
moderate below 20, large above. "+29.6 ± 28.1" is therefore "a small gain",
and "+2.9 ± 4.4" is "a gain" with no size. The measurement is always
`Elo ± half-width, LOS` in logistic Elo; probabilities beyond 99% or below 1%
are worded "above 99%" and "below 1%", never as certainty. Then:

- an unfinished SPRT adds the outlook ("Forecast: 68% chance to pass, with
  roughly 38k more games needed (80% range 13k to 132k)."), or says there are
  too few games to forecast;
- other unfinished Workloads with a target add "N of M games played";
- finished ones add "Based on N games";
- a decided SPRT says which bound it favoured and what that rules out, naming
  the scale of the bounds, which is normalized Elo for a pentanomial test and
  BayesElo for a trinomial one and not the logistic Elo of the measurement
  ("dev is very unlikely to be worth less than 0 normalized Elo"). It then
  gives the measurement with the warning that an SPRT stops the moment it is
  convinced, so the estimate is biased towards the bound it stopped at. That
  holds whichever side of zero the estimate lies.

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
    },
    "results": {                      // null for SPSA
      "verdict": {
        "kind": "likely_gain",        // see the Verdict table
        "tone": "positive",           // positive | negative | neutral
        "text": "Likely a gain: +2.9 ± 4.4 Elo, LOS 90%. Forecast: 68% chance to pass, ..."
      },
      "outcomes": {
        "trinomial_fractions": [0.168, 0.655, 0.177],                 // [L, D, W], or null
        "pentanomial_fractions": [0.049, 0.239, 0.406, 0.258, 0.048], // or null
        "decisive_game_rate": 0.345, "games_per_decisive": 2.9,       // or null
        "swept_pair_rate": 0.096, "level_pair_rate": 0.406,           // or null
        "pair_variance": {            // or null
          "observed": 0.0552, "independent": 0.0431, "ratio": 1.28,
          "game_correlation": 0.28, "pair_efficiency": 0.78
        }
      },
      "outlook": {                    // null unless an unfinished SPRT with enough data
        "pass_probability": 0.684,
        "remaining_games": { "lower": 12820, "median": 38292, "upper": 132442 },
        "interval": 0.8,
        "prior": { "mean_elo": 0.0, "sd_elo": 4.0, "equivalent_games": 7544 }   // normalized Elo
      },
      "speed": {                      // null until a worker has reported node counters
        "overall": {
          "difference": 0.0101,       // spread.mean when there is a spread, else speed.difference
          "speed": { "dev_nps": 2071000, "base_nps": 2050000,
                     "dev_nps_scaled": 2071000, "base_nps_scaled": 2050000,   // each or null
                     "difference": 0.0102 },                                  // or null
          "spread": { "hosts": 5, "mean": 0.0101, "lower": -0.014, "upper": 0.035,
                      "beyond_noise": false }                                 // or null
        },
        "cpus": [ { "cpu_name": "Apple M4", "reading": { "difference": 0.02, "speed": { ... }, "spread": null } } ]
      },
      "consistency": {
        "min_samples": 200, "alpha": 0.001,
        "cpus": [
          { "cpu_name": "AMD Ryzen 9 7950X 16-Core Processor", "hosts": 2,
            "stats": {
              "games": 3296, "pairs": 1648, "elo": Elo,
              "deviation": { "z_score": -1.31, "p_value": 0.19,
                             "adjusted_p_value": 0.76, "flagged": false },    // or null
              "crashes": 4, "timelosses": 19,
              "crash_rate": 0.0012, "timeloss_rate": 0.0058,                  // or null
              "crash_flagged": true, "timeloss_flagged": true,
              "speed": { ... }        // as speed.overall.speed
            } }
        ],
        "heterogeneity": { "statistic": 3.0, "degrees_of_freedom": 3, "p_value": 0.392,
                           "pooled_small_groups": 0, "flagged": false },      // or null
        "hosts": {
          "total": 5, "tested": 5,
          "flagged": [
            { "owner": "home-worker", "machine_name": "demo-2",  // null when the Client sent none
              "machine_label": "demo-2", "pool": "demo-*",
              "cpu_name": "AMD Ryzen 9 7950X 16-Core Processor",
              "machine_id": 2,        // the newest Machine row of the host
              "machines": 1,          // Machine rows merged into it
              "stats": { ... } }      // as a cpu's stats
          ]
        }
      }
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
points; `OpenBench.stats.Elo` dominates). The results add 9 to 39 ms as first
measured, about 12 ms after trimming, for the outlook of an unfinished SPRT
(numpy over 61 drifts, about 60 survival evaluations), and one Elo interval
per CPU and per flagged host. The server endpoint runs a fixed
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
  | `draw_adjudication` | Drawn, at least `movenumber` full moves were completed (counted from the `FEN`'s move number, as fastchess does), and the match runner's draw counter had reached `2 × movecount` plies at the end: consecutive scores within `score` of zero since the last capture or pawn move, that move included (Draw ADJ.). |
  | `draw_by_rule` | Any other draw: repetition, the fifty-move rule, stalemate, insufficient material or a tablebase adjudication. |

  The draw split is an estimate. Captures and pawn moves are read off the
  move text, and a repetition, stalemate or fifty-move draw that happens on the very
  ply the counter fills is counted as an adjudication; a review sample of 199
  real drawn games had 2 (about one in a hundred) in the wrong row, both repetitions. The decisive split has no such ambiguity between
  mate and adjudication, but cannot separate the causes inside
  `unexplained_win`.

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
  Openings played in fewer than two pairs are left out of all three (with a
  large book most openings occur once, and one pair says nothing), so a list
  can be empty and its table is then not shown; `repeated` counts the
  openings that qualify. `lopsided` ranks by `|2 × dev_score − 1| × pairs` (dev's net pairs won or
  lost there), `colour_bound` by the number of `WL` pairs, `drawn` lists
  openings whose pairs were all `DD`, most pairs
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
the first byte (so does a cursor that no longer sits on a member boundary;
see the limits below). The watcher calls the analysis after it has flagged the PGN
rows and deleted the batch files, inside a guard that logs any exception: a
failing analysis never stops archiving.

Limits, each reported under `limits` and named in the page's "Partial" line:

- `complete` is false while a budget stopped a pass before the end; the page
  then asks again every 4 s, and each request advances by one budget.
- A member that is not valid bzip2, is truncated, expands beyond 8 MiB of
  text, or takes longer than the pass's time budget on its own is a
  `damaged_members` entry; the games read before the damage are kept. The
  time and size are checked every 64 lines while the member is read, so one
  hostile upload cannot hold the watcher thread.
- A member still being appended ends the pass without moving the cursor. The
  watcher overwrites the archive's zero padding in place, so a member whose
  header is written but whose data is not yet can look complete and damaged:
  a damaged member that is the last in the archive is therefore not counted
  and not passed until the archive has been unmodified for 30 s.
- Bytes at the cursor that are not a tar header, in a settled archive, mean
  either a corrupt tail or an archive that was replaced by a larger one. A
  walk over the headers from the start tells them apart: if it does not land
  on the cursor the analysis restarts from the first byte, otherwise the pass
  ends there.
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
        "tracked": 16, "repeated": 16, "always_drawn": 1,
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
  - Results, between the tiles and the charts, when `results` is not null:
    the verdict line with its tone on the left edge; tiles for the forecast
    chance to pass (with a meter) and games to decide (both labelled
    "forecast", stating the prior, and absent when `outlook` is null), pair
    variance against independent games with what one pair is worth in
    independent pairs, decisive games, and dev search speed with whether it is
    beyond noise; an "About the forecast" disclosure under the tiles holding
    the caveat in plain text, so it does not depend on hovering; a pair
    outcome table whose share column carries a bar per bucket, coloured from
    loss through level to win (game outcomes for a trinomial Workload); a
    "Consistency by CPU" table with Elo, the z-score against the rest with its adjusted
    p-value printed under it, crashes, time losses and dev speed with its
    noise verdict, flags as labelled badges; and a "Hosts that stand out"
    table that appears only when a host is flagged. The header note states the
    heterogeneity test's result and how many hosts were flagged.
    When the [Games](#games) section is showing (the Workload uploads PGNs and
    a report has arrived), the pair outcome table is left out and a line links
    to Games instead: its pair table is the same distribution with the level
    bucket split into `DD` and `WL`, and two tables of pair outcomes on one
    page, one over every pair and one over the archived pairs, would read as
    a contradiction. The tiles keep using the Workload's own counters.
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
  times the Student t quantile, applied in log space. The degrees of freedom
  are the Kish effective sample size less one, `(Σ w)² / Σ w² − 1` (rounded
  down to a hundredth, which only widens): with equal host times that is
  `n − 1`, and when one always-on machine supplies most of the time next to a
  few short batch jobs it falls towards zero, because the mean is then
  essentially that one machine's ratio. With `n − 1` the interval covered the
  truth only 75–90% of the time under unequal times; with the effective value
  it is conservative (a seeded simulation in the tests checks coverage). There
  is no interval (`lower` and `upper` are `null`) with one host, or when the
  effective degrees of freedom are below 0.5 ("one host dominates"). It stays
  rough: hosts are few, and it assumes each host is an independent draw.
- **Classes.** The step's speed pools every chained class. The same ratio is
  also computed per class (`speed.classes`); when two classes that both have
  intervals differ by more than their margins in quadrature, `classes_differ`
  is true and the lineage row shows the classes next to the pooled figure.
  Otherwise only the pooled figure is shown.
- **Chain.** Along the window's trunk the log ratios add (the ratios
  multiply): the cumulative ratio at step `k` is the engine's speed relative to
  the window's origin commit, shown as a percentage change. Log half-widths add
  in quadrature, like the Elo chain. A step without speed is a gap that adds
  nothing (`measured` of `steps`). Once a step without an interval is in
  the product, the cumulative interval is `null` from there on (`unbounded`
  counts steps without an interval): a band that left their uncertainty out would be too
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
  counters. Each side is charged its own `Threads` (`dev_time` × dev threads
  plus `base_time` × base threads), so a test of one thread against four is
  not charged four on both. `counted_games` is the games of the Results that
  have counters. Whether a Result's counters cover all of its games cannot be
  determined: the worker reports counters per batch alongside the results, and
  a Result only stores running totals, so a Result with counters is taken as
  fully covered. An estimate from games × time control was rejected as the
  primary measure: it needs a guess at moves per game and at how much of the
  clock is used, and the counters are measured.

Window summaries (`economics`):

- **Buckets** `trunk`, `failed`, `other`: the trunk steps; candidates with a
  `failed` verdict at any class; the remaining candidates (still running,
  stopped, or passed but not built on). Each has steps, runs, games, search
  core-hours (`core_hours`, measured only), and its share of the window's
  games. Missing counters are not zero hours: `estimated_core_hours` fills
  them in per run. A run with counters on some of its games is scaled up by
  `games / counted_games`; a run with none is charged its games times the
  window's measured hours per counted game **at its class** (STC and LTC
  differ about fivefold, so one overall rate would be wrong). When a class has
  no measured run at all there is no rate, the bucket's estimate is `null`,
  and `core_share` is `null` for every bucket. `core_share` is the bucket's
  share of the estimated hours; `core_hours_estimated` says an estimate was
  needed. "Spent on failed changes" is the failed bucket's `core_share` with
  its share of games beside it, marked "estimated" with the counter coverage
  when it is; without a usable estimate it falls back to the games share. A trunk step's
  failed repeats count with the trunk: the bucket is the change's fate, not
  the run's. `counter_coverage` is counted games over games.
- **By class** (`classes`): decided SPRT runs `passed` and `failed`, the pass
  rate `passed / (passed + failed)`, the median games of a passed run and of a
  failed run, and all games and core-hours at the class (GAMES runs and
  undecided runs included in those two).
- **Games per Elo**: the games of **finished** runs at the class in the
  window (`finished_games`: trunk and candidates, so the failures are paid
  for) divided by the chained Elo of the window's trunk at that class; `null`
  unless the chained value is positive. A running run is left out on both
  sides: its estimate is provisional and not in the chain, so counting its
  games would charge for Elo not yet credited.
  The chained value is biased upwards by selection (see Chained estimate), so
  the figure is optimistic; the tile says so, and says when the chained total
  is within its margin of zero.

### Cadence

- A step is **settled** when none of its runs is still running or pending.
  Only settled trunk steps count below: a step that passed STC while its LTC
  confirmation runs is not accepted yet.
- A settled trunk step **joins** at the finish of its newest passed run; one
  that was built on without a pass joins at its newest finish. `weekly` counts
  joins per UTC week over the window (zeros included).
- `span_days` is the days from the later of the window's start and the day the
  window's first trunk step was first tested, to today. `steps_per_week` is
  joins × 7 / `span_days`, and `null` while the span is under seven days: two
  steps on the first day are not "14 a week", so the tile then reads "2 steps
  in 1 day".
- **First test to accepted**: for settled steps with a passed run, newest pass
  finish minus the creation of the step's first run (queueing included).
  Median and mean.
- **STC pass to LTC pass**: for steps passed at both, the first LTC pass that
  finished after the first STC pass, minus that STC pass. Median, shown in the
  same tile.

### Cost

Seven queries whatever the data size (six for `all`, which needs no baseline),
each an aggregate or a bounded row set: the usage (Results of the engine's
runs that have counters, grouped by test and host, one tuple per pair, summing
games and the four counters; hosts are merged once per step and class. With
300 tests of 300 hosts each (90,000 pairs) the uncached report takes about a
second, half of it this query), the runs (one row per SPRT or GAMES
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
                  "estimated_core_hours": 2520.2,   // core_hours with uncounted games filled in; null without a rate
                  "games_share": 0.803,       // of the three buckets; null when the total is zero
                  "core_share": 0.903 },      // share of estimated hours; null when any bucket has no estimate
      "failed": { /* as trunk: candidates with a failed verdict */ },
      "other":  { /* as trunk: the remaining candidates */ },
      "counter_coverage": 1.0,        // counted games / games, or null
      "core_hours_estimated": false,  // true when core_share rests on estimated hours
      "classes": [                    // classes tested in the window, in class order
        { "time_class": "stc",
          "passed": 4, "failed": 1,   // decided SPRT runs
          "pass_rate": 0.8,           // or null
          "median_games_to_pass": 24750.0,    // or null
          "median_games_to_fail": 36800.0,    // or null
          "games": 146000, "core_hours": 1018.9,   // every run at the class
          "finished_games": 144200,   // games of finished runs: the numerator of games_per_elo
          "chained_elo": { "lower": 7.7, "value": 12.0, "upper": 16.3 },   // the window's trunk, or null
          "games_per_elo": 12041.4 }  // finished_games / chained value; null unless that is positive
      ],
      "speed": {
        "points": [                   // one per step in lineage.steps
          { "index": 1,
            "step": { "ratio": 0.9941, "lower": 0.9911, "upper": 0.9971, "hosts": 5, "games": 47500 },   // or null
            "cumulative": { "ratio": 0.9941, "lower": 0.9911, "upper": 0.9971 } }   // null at a gap
        ],
        "total": { "ratio": 0.9437, "lower": 0.9400, "upper": 0.9474 },   // whole window, or null
        "measured": 3,                // steps with a speed
        "unbounded": 0,               // of those, steps without an interval (total bounds are then null)
        "steps": 3 },
      "cadence": {
        "weekly": [ { "week_start": "2026-09-28", "steps": 2 } ],   // every week of the window
        "joined": 2,                  // settled trunk steps
        "span_days": 5,
        "steps_per_week": null,       // null under seven days of span
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
spent on failed changes, games per Elo at STC and LTC, trunk velocity, and the
two latencies in one tile, six in all so the row has no orphan), two short charts (speed along the trunk, on the same step axis
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
