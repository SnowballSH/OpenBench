import heapq
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from OpenBench.games.aggregate import OPENING_FIELDS, PAIR_KINDS, Aggregate
from OpenBench.games.domain import (
    ADVANTAGE_THRESHOLDS_CP,
    HISTOGRAM_BIN_PLIES,
    MIN_PAIRS_FOR_OPENING_TABLES,
    OPENING_TABLE_ROWS,
    PHASES,
    TERMINATION_LABELS,
    Colour,
    Outcome,
    Side,
    Termination,
)
from OpenBench.models import GameAnalysis


@dataclass(frozen=True, slots=True)
class Limits:
    complete: bool
    analysed_bytes: int
    archive_bytes: int
    members: int
    damaged_members: int
    malformed_games: int
    unfinished_games: int
    unpaired_games: int
    untracked_opening_pairs: int


@dataclass(frozen=True, slots=True)
class ColourRecord:
    wins: int
    draws: int
    losses: int
    score: float | None


@dataclass(frozen=True, slots=True)
class ColourResults:
    dev_as_white: ColourRecord
    dev_as_black: ColourRecord
    white: ColourRecord


@dataclass(frozen=True, slots=True)
class PairResults:
    total: int
    ww: int
    wd: int
    wl: int
    dd: int
    dl: int
    ll: int
    pentanomial: list[int]
    middle_wl_share: float | None
    white_sweeps: int
    black_sweeps: int


@dataclass(frozen=True, slots=True)
class TerminationRow:
    key: Termination
    label: str
    games: int
    share: float


@dataclass(frozen=True, slots=True)
class Terminations:
    inferred_games: int
    rows: list[TerminationRow]


@dataclass(frozen=True, slots=True)
class LengthSummary:
    games: int
    mean: float | None
    q1: float | None
    median: float | None
    q3: float | None
    longest: int | None


@dataclass(frozen=True, slots=True)
class LengthBin:
    first_ply: int
    last_ply: int
    decisive: int
    drawn: int


@dataclass(frozen=True, slots=True)
class Lengths:
    all: LengthSummary
    decisive: LengthSummary
    drawn: LengthSummary
    bin_plies: int
    histogram: list[LengthBin]


@dataclass(frozen=True, slots=True)
class OpeningRow:
    opening: str
    pairs: int
    ww: int
    wd: int
    wl: int
    dd: int
    dl: int
    ll: int
    white_sweeps: int
    black_sweeps: int
    dev_score: float


@dataclass(frozen=True, slots=True)
class Openings:
    tracked: int
    repeated: int
    always_drawn: int
    lopsided: list[OpeningRow]
    colour_bound: list[OpeningRow]
    drawn: list[OpeningRow]


@dataclass(frozen=True, slots=True)
class BookBalance:
    games: int
    mean_white_cp: float
    mean_abs_cp: float


@dataclass(frozen=True, slots=True)
class AdvantageRow:
    side: Side
    threshold_cp: int
    reached: int
    won: int
    drawn: int
    lost: int
    not_won_share: float | None


@dataclass(frozen=True, slots=True)
class PhaseUse:
    moves: int
    mean_depth: float | None
    timed_moves: int
    nps: float | None
    mean_time_ms: float | None


@dataclass(frozen=True, slots=True)
class PhaseRow:
    key: str
    label: str
    dev: PhaseUse
    base: PhaseUse


@dataclass(frozen=True, slots=True)
class Evals:
    games: int
    book: BookBalance
    advantage: list[AdvantageRow]
    phases: list[PhaseRow]
    has_timing: bool


@dataclass(frozen=True, slots=True)
class GameReport:
    updated_at: datetime
    games: int
    limits: Limits
    colour: ColourResults
    pairs: PairResults
    terminations: Terminations
    lengths: Lengths
    openings: Openings
    evals: Evals | None


def ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def colour_record(wins: int, draws: int, losses: int) -> ColourRecord:
    return ColourRecord(wins, draws, losses, ratio(wins + draws / 2, wins + draws + losses))


def colour_results(colour: Mapping[str, int]) -> ColourResults:

    def count(side: Colour, outcome: Outcome) -> int:
        return colour.get(f'{side}.{outcome}', 0)

    as_white = [count(Colour.WHITE, outcome) for outcome in Outcome]
    as_black = [count(Colour.BLACK, outcome) for outcome in Outcome]
    return ColourResults(
        dev_as_white=colour_record(*as_white),
        dev_as_black=colour_record(*as_black),
        white=colour_record(as_white[0] + as_black[2], as_white[1] + as_black[1], as_white[2] + as_black[0]),
    )


def pair_results(aggregate: Aggregate) -> PairResults:
    ww, wd, wl, dd, dl, ll = (aggregate.pairs.get(kind, 0) for kind in PAIR_KINDS)
    return PairResults(
        total=ww + wd + wl + dd + dl + ll,
        ww=ww,
        wd=wd,
        wl=wl,
        dd=dd,
        dl=dl,
        ll=ll,
        pentanomial=[ll, dl, dd + wl, wd, ww],
        middle_wl_share=ratio(wl, wl + dd),
        white_sweeps=aggregate.sweeps.get(Colour.WHITE, 0),
        black_sweeps=aggregate.sweeps.get(Colour.BLACK, 0),
    )


def terminations(aggregate: Aggregate) -> Terminations:
    games = sum(aggregate.terminations.values())
    rows = [
        TerminationRow(Termination(key), TERMINATION_LABELS[Termination(key)], count, count / games)
        for key, count in aggregate.terminations.items()
        if count
    ]
    return Terminations(
        inferred_games=aggregate.totals['inferred_terminations'],
        rows=sorted(rows, key=lambda row: (-row.games, row.key)),
    )


def ply_at(plies: list[tuple[int, int]], rank: int) -> int:
    seen = 0
    for ply, count in plies:
        seen += count
        if seen > rank:
            return ply
    return plies[-1][0]


def quantile(plies: list[tuple[int, int]], games: int, fraction: float) -> float:
    rank = fraction * (games - 1)
    lower = int(rank)
    low, high = ply_at(plies, lower), ply_at(plies, min(lower + 1, games - 1))
    return low + (rank - lower) * (high - low)


def length_summary(counts: Mapping[int, int]) -> LengthSummary:

    plies = sorted(counts.items())
    if not (games := sum(counts.values())):
        return LengthSummary(0, None, None, None, None, None)

    return LengthSummary(
        games=games,
        mean=sum(ply * count for ply, count in plies) / games,
        q1=quantile(plies, games, 0.25),
        median=quantile(plies, games, 0.5),
        q3=quantile(plies, games, 0.75),
        longest=plies[-1][0],
    )


def by_ply(counts: Mapping[str, int]) -> Counter[int]:
    return Counter({int(ply): count for ply, count in counts.items()})


def histogram(decisive: Mapping[int, int], drawn: Mapping[int, int]) -> list[LengthBin]:

    def binned(counts: Mapping[int, int]) -> Counter[int]:
        bins: Counter[int] = Counter()
        for ply, count in counts.items():
            bins[max(0, ply - 1) // HISTOGRAM_BIN_PLIES] += count
        return bins

    decisive_bins, drawn_bins = binned(decisive), binned(drawn)
    if not (used := decisive_bins.keys() | drawn_bins.keys()):
        return []

    return [
        LengthBin(
            index * HISTOGRAM_BIN_PLIES + 1, (index + 1) * HISTOGRAM_BIN_PLIES, decisive_bins[index], drawn_bins[index]
        )
        for index in range(max(used) + 1)
    ]


def lengths(aggregate: Aggregate) -> Lengths:
    decisive, drawn = by_ply(aggregate.decisive_plies), by_ply(aggregate.drawn_plies)
    return Lengths(
        all=length_summary(decisive + drawn),
        decisive=length_summary(decisive),
        drawn=length_summary(drawn),
        bin_plies=HISTOGRAM_BIN_PLIES,
        histogram=histogram(decisive, drawn),
    )


def opening_row(opening: str, row: list[int]) -> OpeningRow:
    counts = dict(zip(OPENING_FIELDS, row, strict=True))
    pairs = sum(counts[kind] for kind in PAIR_KINDS)
    points = 2 * counts['WW'] + 1.5 * counts['WD'] + counts['WL'] + counts['DD'] + 0.5 * counts['DL']
    return OpeningRow(
        opening=opening,
        pairs=pairs,
        ww=counts['WW'],
        wd=counts['WD'],
        wl=counts['WL'],
        dd=counts['DD'],
        dl=counts['DL'],
        ll=counts['LL'],
        white_sweeps=counts[Colour.WHITE],
        black_sweeps=counts[Colour.BLACK],
        dev_score=points / (2 * pairs),
    )


def dev_margin(row: OpeningRow) -> float:
    return abs(2 * row.dev_score - 1) * row.pairs


def top_rows(rows: Iterable[OpeningRow], weight: dict[str, float]) -> list[OpeningRow]:
    ranked = heapq.nsmallest(OPENING_TABLE_ROWS, rows, key=lambda row: (-weight[row.opening], row.opening))
    return [row for row in ranked if weight[row.opening] > 0]


def openings(aggregate: Aggregate) -> Openings:

    rows = [opening_row(opening, row) for opening, row in aggregate.openings.items()]
    repeated = [row for row in rows if row.pairs >= MIN_PAIRS_FOR_OPENING_TABLES]
    always_drawn = [row for row in repeated if row.dd == row.pairs]

    return Openings(
        tracked=len(rows),
        repeated=len(repeated),
        always_drawn=len(always_drawn),
        lopsided=top_rows(repeated, {row.opening: dev_margin(row) for row in repeated}),
        colour_bound=top_rows(repeated, {row.opening: row.wl for row in repeated}),
        drawn=top_rows(always_drawn, {row.opening: row.pairs for row in always_drawn}),
    )


def advantage_rows(advantage: Mapping[str, int]) -> list[AdvantageRow]:

    def row(side: Side, threshold: int) -> AdvantageRow:
        won, drawn, lost = (advantage.get(f'{side}.{threshold}.{outcome}', 0) for outcome in Outcome)
        reached = won + drawn + lost
        return AdvantageRow(side, threshold, reached, won, drawn, lost, ratio(drawn + lost, reached))

    return [row(side, threshold) for threshold in ADVANTAGE_THRESHOLDS_CP for side in Side]


def phase_use(usage: Mapping[str, int], side: Side, phase: str) -> PhaseUse:

    def total(name: str) -> int:
        return usage.get(f'{side}.{phase}.{name}', 0)

    seconds = total('time_ms') / 1000
    return PhaseUse(
        moves=total('depth_moves'),
        mean_depth=ratio(total('depth'), total('depth_moves')),
        timed_moves=total('timed_moves'),
        nps=ratio(total('nodes'), seconds),
        mean_time_ms=ratio(total('time_ms'), total('timed_moves')),
    )


def evals(aggregate: Aggregate) -> Evals | None:

    if not (games := aggregate.totals['scored_games']):
        return None

    phases = [
        PhaseRow(
            phase.key,
            phase.label,
            phase_use(aggregate.usage, Side.DEV, phase.key),
            phase_use(aggregate.usage, Side.BASE, phase.key),
        )
        for phase in PHASES
    ]
    return Evals(
        games=games,
        book=BookBalance(games, aggregate.totals['book_eval_cp'] / games, aggregate.totals['book_eval_abs_cp'] / games),
        advantage=advantage_rows(aggregate.advantage),
        phases=phases,
        has_timing=any(row.dev.timed_moves or row.base.timed_moves for row in phases),
    )


def game_report(row: GameAnalysis, archive_bytes: int) -> GameReport:
    aggregate = Aggregate.from_state(row.state)
    return GameReport(
        updated_at=row.updated,
        games=row.games,
        limits=Limits(
            complete=row.complete,
            analysed_bytes=row.analysed_bytes,
            archive_bytes=archive_bytes,
            members=row.members,
            damaged_members=aggregate.totals['damaged_members'],
            malformed_games=aggregate.totals['malformed'],
            unfinished_games=aggregate.totals['unfinished'],
            unpaired_games=aggregate.totals['unpaired'],
            untracked_opening_pairs=aggregate.totals['untracked_opening_pairs'],
        ),
        colour=colour_results(aggregate.colour),
        pairs=pair_results(aggregate),
        terminations=terminations(aggregate),
        lengths=lengths(aggregate),
        openings=openings(aggregate),
        evals=evals(aggregate),
    )
