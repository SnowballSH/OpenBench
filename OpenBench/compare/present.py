from collections.abc import Callable, Sequence
from dataclasses import dataclass

from OpenBench.compare.analysis import Difference, difference_series, elo_difference
from OpenBench.insights.domain import WorkloadMode
from OpenBench.insights.eta import EtaKind
from OpenBench.insights.listing import ETA_REASON_TEXT, format_duration, format_rate
from OpenBench.insights.serialize import Json, to_json
from OpenBench.insights.series import SeriesPoint
from OpenBench.insights.strength import EloInterval
from OpenBench.insights.workload import WorkloadInsights
from OpenBench.models import Test
from OpenBench.progress.present import DASH, count, elo_text, fixed, percent

type Cell = Callable[[WorkloadInsights], str]

CHART_DIGITS = 3


@dataclass(frozen=True, slots=True)
class Side:
    key: str
    id: int
    name: str
    url: str
    label: str
    insights: WorkloadInsights


@dataclass(frozen=True, slots=True)
class SummaryRow:
    label: str
    a: str
    b: str


@dataclass(frozen=True, slots=True)
class ComparePage:
    a: Side
    b: Side
    rows: list[SummaryRow]
    difference: str
    show_elo: bool
    show_llr: bool

    @property
    def sides(self) -> tuple[Side, Side]:
        return self.a, self.b


def side(key: str, test: Test, insights: WorkloadInsights) -> Side:
    return Side(
        key=key,
        id=test.id,
        name=str(test),
        url=f'/{test.workload_type_str()}/{test.id}/',
        label=f'#{test.id} {test.dev.name}',
        insights=insights,
    )


def games_text(insights: WorkloadInsights) -> str:
    progress = insights.progress
    target = f' of {count(progress.target_games)}' if progress.target_games else ''
    return f'{count(progress.games)}{target}'


def strength_interval(insights: WorkloadInsights, normalized: bool) -> EloInterval | None:
    strength = insights.strength
    if strength is None:
        return None
    return strength.normalized_elo if normalized else strength.elo


def los_text(insights: WorkloadInsights) -> str:
    return percent(insights.strength.los if insights.strength else None, 1)


def llr_text(insights: WorkloadInsights) -> str:
    progress = insights.progress
    if progress.llr is None or progress.llr_lower is None or progress.llr_upper is None:
        return DASH
    return f'{fixed(progress.llr)} ({fixed(progress.llr_lower)}, {fixed(progress.llr_upper)})'


def elapsed_text(insights: WorkloadInsights) -> str:
    return format_duration(insights.timing.elapsed_seconds) if insights.timing else DASH


def rate_text(insights: WorkloadInsights) -> str:
    rate = insights.timing.best_rate() if insights.timing else None
    return f'{format_rate(rate.games_per_hour)} games/h' if rate else DASH


def time_left_text(insights: WorkloadInsights) -> str:
    eta = insights.eta
    if eta.kind == EtaKind.FINISHED:
        return 'finished'
    if eta.remaining_seconds is not None:
        approximate = '≈ ' if eta.kind == EtaKind.SPRT else ''
        return f'{approximate}{format_duration(eta.remaining_seconds)}'
    return ETA_REASON_TEXT[eta.reason] if eta.reason else DASH


def summary_cells(show_elo: bool, show_llr: bool) -> list[tuple[str, Cell]]:
    cells: list[tuple[str, Cell]] = [
        ('Mode', lambda insights: insights.workload.mode.value),
        ('Status', lambda insights: insights.workload.status.value.capitalize()),
        ('Games', games_text),
    ]
    if show_elo:
        cells += [
            ('Elo (95%)', lambda insights: elo_text(strength_interval(insights, normalized=False))),
            ('Normalized Elo (95%)', lambda insights: elo_text(strength_interval(insights, normalized=True))),
            ('LOS', los_text),
        ]
    if show_llr:
        cells.append(('LLR (bounds)', llr_text))
    cells += [('Elapsed', elapsed_text), ('Games per hour', rate_text), ('Time left', time_left_text)]
    return cells


def difference_text(a: WorkloadInsights, b: WorkloadInsights) -> str:
    difference = elo_difference(strength_interval(a, normalized=False), strength_interval(b, normalized=False))
    return f'≈ {elo_text(difference)}' if difference else DASH


def is_sprt(insights: WorkloadInsights) -> bool:
    return insights.workload.mode == WorkloadMode.SPRT


def compare_page(a: Side, b: Side) -> ComparePage:
    show_elo = a.insights.strength is not None and b.insights.strength is not None
    llr_row = is_sprt(a.insights) or is_sprt(b.insights)
    rows = [SummaryRow(label, cell(a.insights), cell(b.insights)) for label, cell in summary_cells(show_elo, llr_row)]
    return ComparePage(
        a=a,
        b=b,
        rows=rows,
        difference=difference_text(a.insights, b.insights) if show_elo else DASH,
        show_elo=show_elo,
        show_llr=is_sprt(a.insights) and is_sprt(b.insights),
    )


def rounded(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def history_columns(points: Sequence[SeriesPoint]) -> dict[str, Json]:
    return {
        'games': [point.games for point in points],
        'elapsed': [round((point.timestamp - points[0].timestamp).total_seconds()) for point in points],
        'llr': [rounded(point.llr, CHART_DIGITS) for point in points],
        'elo': [rounded(point.elo, CHART_DIGITS) for point in points],
        'elo_lower': [rounded(point.elo_lower, CHART_DIGITS) for point in points],
        'elo_upper': [rounded(point.elo_upper, CHART_DIGITS) for point in points],
    }


def difference_columns(differences: Sequence[Difference]) -> dict[str, Json]:
    return {
        'games': [difference.games for difference in differences],
        'value': [round(difference.value, CHART_DIGITS) for difference in differences],
        'lower': [round(difference.lower, CHART_DIGITS) for difference in differences],
        'upper': [round(difference.upper, CHART_DIGITS) for difference in differences],
    }


def chart_side(entry: Side) -> Json:
    progress = entry.insights.progress
    return {
        'key': entry.key,
        'label': entry.label,
        'llr_lower': progress.llr_lower,
        'llr_upper': progress.llr_upper,
        'history': history_columns(entry.insights.history.points),
    }


def chart_payload(page: ComparePage) -> Json:
    differences = (
        difference_series(page.a.insights.history.points, page.b.insights.history.points) if page.show_elo else []
    )
    return to_json(
        {
            'show_elo': page.show_elo,
            'show_llr': page.show_llr,
            'workloads': [chart_side(page.a), chart_side(page.b)],
            'difference': difference_columns(differences),
        }
    )
