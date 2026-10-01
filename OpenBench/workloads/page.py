from dataclasses import dataclass, fields
from datetime import datetime
from typing import Literal, cast

from django.utils import timezone

from OpenBench.insights.domain import WorkloadFacts, WorkloadMode, WorkloadStatus
from OpenBench.insights.eta import estimate_eta
from OpenBench.insights.listing import RowTiming, eta_parts
from OpenBench.insights.results.analysis import outlook_of
from OpenBench.insights.results.verdict import Verdict, give_verdict
from OpenBench.insights.sources import workload_facts
from OpenBench.insights.strength import summarize_strength
from OpenBench.models import Test
from OpenBench.progress.domain import TimeClass
from OpenBench.workload_names import commit_pair
from OpenBench.workloads.confirmation import workload_time_class

type BadgeVariant = Literal['warn', 'accent', 'pass', 'fail', 'neutral']
type MeterKind = Literal['position', 'fill']

STATE_VARIANTS: dict[WorkloadStatus, BadgeVariant] = {
    WorkloadStatus.PENDING: 'warn',
    WorkloadStatus.ACTIVE: 'accent',
    WorkloadStatus.PASSED: 'pass',
    WorkloadStatus.FAILED: 'fail',
    WorkloadStatus.COMPLETED: 'pass',
    WorkloadStatus.STOPPED: 'neutral',
    WorkloadStatus.DELETED: 'neutral',
}

# The states the stat block's colour says nothing about; any other is named by result_label
STATE_LABELS: dict[WorkloadStatus, str] = {
    WorkloadStatus.COMPLETED: 'Completed',
    WorkloadStatus.STOPPED: 'Stopped',
    WorkloadStatus.DELETED: 'Deleted',
}


@dataclass(frozen=True, slots=True)
class StateBadge:
    label: str | None
    variant: BadgeVariant


# Only an SPRT decides anything: a fixed run that reached its target carries the passed or failed flag of its score
UNDECIDED_STATES: dict[WorkloadStatus, StateBadge] = {
    WorkloadStatus.PASSED: StateBadge('Completed', 'neutral'),
    WorkloadStatus.FAILED: StateBadge('Completed', 'neutral'),
}


@dataclass(frozen=True, slots=True)
class PageHeader:
    title: str
    pair: str
    details: str
    engine: str
    time_class: str | None
    time_control: str
    mode: str
    state: StateBadge


@dataclass(frozen=True, slots=True)
class SummaryMeter:
    kind: MeterKind
    caption: str
    fraction: float
    label: str


@dataclass(frozen=True, slots=True)
class PageSummary:
    verdict: Verdict | None
    meter: SummaryMeter | None
    timing: RowTiming | None


@dataclass(frozen=True, slots=True)
class PageSection:
    id: str
    label: str
    shown: bool


@dataclass(frozen=True, slots=True)
class PageSections:
    results: PageSection | None
    progress: PageSection
    workers: PageSection
    games: PageSection | None
    parameters: PageSection | None
    errors: PageSection | None
    configuration: PageSection
    raw_results: PageSection

    def listed(self) -> list[PageSection]:
        sections = (getattr(self, field.name) for field in fields(self))
        return [section for section in sections if section is not None]


@dataclass(frozen=True, slots=True)
class WorkloadPage:
    header: PageHeader
    summary: PageSummary
    sections: PageSections
    states: dict[str, dict[str, str | None]]


def state_of(status: WorkloadStatus, facts: WorkloadFacts) -> StateBadge:
    if facts.sprt is None and status in UNDECIDED_STATES:
        return UNDECIDED_STATES[status]
    return StateBadge(STATE_LABELS.get(status), STATE_VARIANTS[status])


def state_table(facts: WorkloadFacts) -> dict[str, dict[str, str | None]]:

    # What live.js draws when the status moves; a null label is the payload's own outcome text
    states = ((status, state_of(status, facts)) for status in WorkloadStatus)
    return {status.value: {'label': state.label, 'variant': state.variant} for status, state in states}


def state_badge(workload: Test, facts: WorkloadFacts) -> StateBadge:
    # Imported here, as below: the listing modules load OpenBench.utils, whose views load this module
    from OpenBench.listing_rows import result_label
    from OpenBench.templatetags import mytags

    state = state_of(facts.status, facts)
    colour = cast(str, mytags.testResultColour(workload))
    return StateBadge(state.label or result_label(workload, colour), state.variant)


def versus(dev: str, base: str) -> str:
    return dev if dev == base else f'{dev} vs {base}'


def mode_text(workload: Test, facts: WorkloadFacts) -> str:
    if facts.sprt is not None:
        return f'SPRT [{facts.sprt.elo0:g}, {facts.sprt.elo1:g}]'
    if facts.mode == WorkloadMode.SPSA:
        return 'SPSA tune'
    games = f'{workload.max_games:,} games'
    return f'Datagen, {games}' if facts.mode == WorkloadMode.DATAGEN else f'Fixed {games}'


def time_class_label(workload: Test) -> str | None:
    found = workload_time_class(workload)
    return None if found == TimeClass.OTHER else found.label


def page_header(workload: Test, facts: WorkloadFacts) -> PageHeader:
    from OpenBench.listing_rows import workload_label
    from OpenBench.templatetags import mytags

    label = workload_label(workload, cast(str, mytags.prettyDevName(workload)))
    return PageHeader(
        title=label.title,
        pair=commit_pair(workload),
        details=label.info.strip(),
        engine=versus(workload.dev_engine, workload.base_engine),
        time_class=time_class_label(workload),
        time_control=versus(workload.dev_time_control, workload.base_time_control),
        mode=mode_text(workload, facts),
        state=state_badge(workload, facts),
    )


def has_strength(facts: WorkloadFacts) -> bool:
    return facts.mode != WorkloadMode.SPSA and facts.outcomes.games > 0


def page_verdict(facts: WorkloadFacts) -> Verdict | None:
    if not has_strength(facts):
        return None
    return give_verdict(facts, summarize_strength(facts.outcomes), outlook_of(facts))


def clamped(fraction: float) -> float:
    return min(1.0, max(0.0, fraction))


def summary_meter(facts: WorkloadFacts) -> SummaryMeter | None:

    # Only a running workload is on its way somewhere; a settled one states its result instead
    if facts.status != WorkloadStatus.ACTIVE:
        return None

    if facts.sprt is not None and facts.sprt.upper_llr > facts.sprt.lower_llr:
        lower, upper = facts.sprt.lower_llr, facts.sprt.upper_llr
        fraction = (facts.llr - lower) / (upper - lower)
        label = f'LLR {facts.llr:.2f} between {lower:.2f} and {upper:.2f}'
        return SummaryMeter('position', 'LLR', clamped(fraction), label)

    if facts.target_games:
        games = facts.outcomes.games
        label = f'{games:,} of {facts.target_games:,} games'
        return SummaryMeter('fill', 'Games', clamped(games / facts.target_games), label)

    return None


def idle_timing(facts: WorkloadFacts, now: datetime) -> RowTiming | None:

    # Before the first game there is no rate to read, only the reason there is no time left yet
    if facts.status != WorkloadStatus.ACTIVE:
        return None
    kind, text = eta_parts(estimate_eta(facts, None, now))
    return RowTiming(kind, text) if text else None


def page_timing(workload: Test, facts: WorkloadFacts, now: datetime) -> RowTiming | None:

    from OpenBench.page_queries import listing_row_timing, listing_tests

    if facts.outcomes.games == 0:
        return idle_timing(facts, now)

    # One query: the snapshot marks the index annotates onto its rows, for the same time left and rate
    listed = listing_tests(Test.objects.filter(id=workload.id), now).first()
    return listing_row_timing(listed) if listed else None


def page_sections(workload: Test, facts: WorkloadFacts, worker_errors: int) -> PageSections:

    played = facts.outcomes.games > 0
    return PageSections(
        results=PageSection('results', 'Results', played) if facts.mode != WorkloadMode.SPSA else None,
        progress=PageSection('progress', 'Progress', played),
        workers=PageSection('workers', 'Workers', played),
        games=PageSection('games', 'Games', False) if workload.upload_pgns != 'FALSE' else None,
        parameters=PageSection('parameters', 'Parameters', True) if facts.mode == WorkloadMode.SPSA else None,
        errors=PageSection('errors', f'Errors ({worker_errors})', True) if worker_errors else None,
        configuration=PageSection('configuration', 'Configuration', True),
        raw_results=PageSection('raw-results', 'Raw results', True),
    )


def workload_page(workload: Test, worker_errors: int, now: datetime | None = None) -> WorkloadPage:
    facts = workload_facts(workload)
    return WorkloadPage(
        header=page_header(workload, facts),
        summary=PageSummary(
            verdict=page_verdict(facts),
            meter=summary_meter(facts),
            timing=page_timing(workload, facts, now or timezone.now()),
        ),
        sections=page_sections(workload, facts, worker_errors),
        states=state_table(facts),
    )
