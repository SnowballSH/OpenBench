from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import groupby

from OpenBench.digest.domain import MOST_BUCKETS, POOLS_SENT, DigestWindow, FleetActivity, GamesBucket, PoolActivity
from OpenBench.digest.sources import HostRow, HourMaximum
from OpenBench.fleet.pools import Pool, pool_label
from OpenBench.fleet.status import UNKNOWN
from OpenBench.progress.domain import Run, RunRow, TimeClass
from OpenBench.progress.economics import Rates, class_rates
from OpenBench.progress.lineage import run_of

BUCKET_HOURS = (1, 2, 3, 4, 6, 8, 12, 24)
HOUR = timedelta(hours=1)

type ClassedRun = tuple[TimeClass, Run]
type HourGames = dict[tuple[int, datetime], int]


@dataclass(frozen=True, slots=True)
class CoreHours:
    hours: float | None
    estimated: bool
    games_without: int


def bucket_hours(span: timedelta) -> int:
    hours = span / HOUR
    return next((size for size in BUCKET_HOURS if hours / size <= MOST_BUCKETS), BUCKET_HOURS[-1])


def bucket_start(moment: datetime, hours: int) -> datetime:
    start = moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return start - timedelta(hours=start.hour % hours)


def bucket_starts(window: DigestWindow, hours: int) -> list[datetime]:
    first, last = bucket_start(window.since, hours), bucket_start(window.until, hours)
    step = timedelta(hours=hours)
    return [first + index * step for index in range((last - first) // step + 1)]


def games_by_hour(maxima: Iterable[HourMaximum], baselines: Mapping[int, int]) -> HourGames:
    played: HourGames = {}
    ordered = sorted(maxima, key=lambda row: (row[0], row[1]))
    for test_id, rows in groupby(ordered, key=lambda row: row[0]):
        previous = baselines.get(test_id, 0)
        for _, hour, games in rows:
            played[test_id, hour] = max(0, games - previous)
            previous = max(previous, games)
    return played


def games_by_test(played: HourGames) -> dict[int, int]:
    totals: Counter[int] = Counter()
    for (test_id, _), games in played.items():
        totals[test_id] += games
    return dict(totals)


def classed_runs(runs: Iterable[RunRow]) -> dict[int, ClassedRun]:
    return {row.id: (row.time_class, run_of(row)) for row in runs}


def class_of(test_id: int, runs: Mapping[int, ClassedRun]) -> TimeClass:
    found = runs.get(test_id)
    return found[0] if found else TimeClass.OTHER


def games_buckets(
    played: HourGames, runs: Mapping[int, ClassedRun], window: DigestWindow, hours: int
) -> tuple[list[TimeClass], list[GamesBucket]]:
    totals: defaultdict[datetime, Counter[TimeClass]] = defaultdict(Counter)
    for (test_id, hour), games in played.items():
        totals[bucket_start(hour, hours)][class_of(test_id, runs)] += games
    seen = {time_class for counts in totals.values() for time_class, games in counts.items() if games}
    classes = [time_class for time_class in TimeClass if time_class in seen]
    return classes, [
        GamesBucket(start, [totals[start][time_class] for time_class in classes])
        for start in bucket_starts(window, hours)
    ]


def window_hours(games: int, found: ClassedRun | None, rates: Rates) -> tuple[float | None, bool]:
    if found is None:
        return None, False
    time_class, run = found
    if run.core_hours is not None and run.counted_games:
        return run.core_hours * games / run.counted_games, run.counted_games < run.games
    rate = rates.get(time_class)
    return (None, False) if rate is None else (rate * games, True)


def core_hours(played: Mapping[int, int], runs: Mapping[int, ClassedRun]) -> CoreHours:
    rates = class_rates(runs.values())
    spent = [(games, *window_hours(games, runs.get(test_id), rates)) for test_id, games in played.items() if games]
    known = [hours for _, hours, _ in spent if hours is not None]
    return CoreHours(
        hours=sum(known) if known or not spent else None,
        estimated=any(estimated for _, _, estimated in spent),
        games_without=sum(games for games, hours, _ in spent if hours is None),
    )


def named(value: object) -> str | None:
    return value if isinstance(value, str) and value not in ('', 'None') else None


def host_pool(owner: str, machine_name: object, cpu_name: object) -> Pool:
    cpu = named(cpu_name)
    return Pool(owner, pool_label(named(machine_name), cpu), cpu or UNKNOWN)


def pool_activity(hosts: Iterable[HostRow]) -> tuple[int, list[PoolActivity]]:
    pools = {(owner, host or f'{name}/{cpu}'): host_pool(owner, name, cpu) for owner, host, name, cpu in hosts}
    counts = Counter(pools.values())
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0].label, item[0].owner))
    return len(pools), [PoolActivity(pool.owner, pool.label, pool.cpu_name, total) for pool, total in ranked]


def peak(buckets: Sequence[GamesBucket], hours: int) -> tuple[float | None, datetime | None]:
    busiest = max(buckets, key=lambda bucket: bucket.total, default=None)
    if busiest is None or not busiest.total:
        return None, None
    return busiest.total / hours, busiest.start


def fleet_activity(
    window: DigestWindow,
    maxima: Iterable[HourMaximum],
    baselines: Mapping[int, int],
    runs: Iterable[RunRow],
    hosts: Iterable[HostRow],
) -> FleetActivity:
    played = games_by_hour(maxima, baselines)
    by_test = games_by_test(played)
    known = classed_runs(runs)
    hours = bucket_hours(window.span)
    classes, buckets = games_buckets(played, known, window, hours)
    spent = core_hours(by_test, known)
    host_count, pools = pool_activity(hosts)
    peak_rate, peak_at = peak(buckets, hours)
    return FleetActivity(
        games=sum(by_test.values()),
        core_hours=spent.hours,
        core_hours_estimated=spent.estimated,
        games_without_hours=spent.games_without,
        hosts=host_count,
        pools=pools[:POOLS_SENT],
        pools_omitted=max(0, len(pools) - POOLS_SENT),
        bucket_hours=hours,
        classes=classes,
        buckets=buckets,
        peak_games_per_hour=peak_rate,
        peak_at=peak_at,
    )
