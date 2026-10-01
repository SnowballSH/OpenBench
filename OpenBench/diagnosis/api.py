from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from enum import StrEnum

from django.db.models import QuerySet

from OpenBench.diagnosis.domain import DiagnosisState
from OpenBench.diagnosis.report import diagnose_workload, diagnose_workloads
from OpenBench.insights.domain import WorkloadMode, WorkloadStatus
from OpenBench.insights.serialize import Json, to_json
from OpenBench.insights.sources import workload_facts
from OpenBench.insights.strength import EloInterval, elo_interval
from OpenBench.insights.workload import workload_insights
from OpenBench.models import Engine, Test

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_ID_DIGITS = 18


class StatusFilter(StrEnum):
    ACTIVE = 'active'
    PENDING = 'pending'
    FINISHED = 'finished'
    ALL = 'all'


STATUS_ERROR = f'status must be one of {", ".join(status.value for status in StatusFilter)}'
LIMIT_ERROR = f'limit must be a whole number from 1 to {MAX_LIMIT}'
SINCE_ERROR = 'since_id must be a whole number'


class BadQuery(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ListingQuery:
    status: StatusFilter = StatusFilter.ALL
    engine: str | None = None
    since_id: int | None = None
    limit: int = DEFAULT_LIMIT


@dataclass(frozen=True, slots=True)
class EngineRef:
    name: str
    sha: str


@dataclass(frozen=True, slots=True)
class DiagnosisRef:
    state: DiagnosisState
    headline: str


@dataclass(frozen=True, slots=True)
class WorkloadRow:
    id: int
    mode: WorkloadMode
    status: WorkloadStatus
    engine: str
    dev: EngineRef
    base: EngineRef
    time_control: str
    games: int
    llr: float | None
    llr_lower: float | None
    llr_upper: float | None
    elo: EloInterval | None
    created_at: datetime
    updated_at: datetime
    info: str
    diagnosis: DiagnosisRef


def insights_payload(workload: Test) -> dict[str, Json]:
    insights = workload_insights(workload)
    return {
        **{field.name: to_json(getattr(insights, field.name)) for field in fields(insights)},
        'diagnosis': to_json(diagnose_workload(workload, insights.generated_at)),
    }


def whole_number(raw: str, error: str) -> int:
    if not (raw.isascii() and raw.isdigit() and len(raw) <= MAX_ID_DIGITS):
        raise BadQuery(error)
    return int(raw)


def parse_query(params: Mapping[str, str]) -> ListingQuery:

    try:
        status = StatusFilter(params.get('status') or StatusFilter.ALL)
    except ValueError:
        raise BadQuery(STATUS_ERROR) from None

    limit = whole_number(params['limit'], LIMIT_ERROR) if params.get('limit') else DEFAULT_LIMIT
    if not 1 <= limit <= MAX_LIMIT:
        raise BadQuery(LIMIT_ERROR)

    return ListingQuery(
        status=status,
        engine=params.get('engine') or None,
        since_id=whole_number(params['since_id'], SINCE_ERROR) if params.get('since_id') else None,
        limit=limit,
    )


def with_status(workloads: QuerySet[Test], status: StatusFilter) -> QuerySet[Test]:
    if status == StatusFilter.ACTIVE:
        return workloads.filter(approved=True, finished=False, deleted=False)
    if status == StatusFilter.PENDING:
        return workloads.filter(approved=False, finished=False, deleted=False)
    if status == StatusFilter.FINISHED:
        return workloads.filter(finished=True, deleted=False)
    return workloads


def listed_workloads(query: ListingQuery) -> list[Test]:

    workloads = with_status(Test.objects.select_related('dev', 'base', 'spsa_run'), query.status)
    if query.engine is not None:
        workloads = workloads.filter(dev_engine=query.engine)

    # A cursor walks forward from since_id; without one the newest workloads come first
    if query.since_id is not None:
        return list(workloads.filter(id__gt=query.since_id).order_by('id')[: query.limit])
    return list(workloads.order_by('-id')[: query.limit])


def engine_ref(engine: Engine) -> EngineRef:
    return EngineRef(engine.name, engine.sha)


def first_line(text: str) -> str:
    return text.strip().partition('\n')[0].strip()


def workload_row(workload: Test, diagnosis: DiagnosisRef) -> WorkloadRow:

    facts = workload_facts(workload)
    return WorkloadRow(
        id=workload.id,
        mode=facts.mode,
        status=facts.status,
        engine=workload.dev_engine,
        dev=engine_ref(workload.dev),
        base=engine_ref(workload.base),
        time_control=workload.dev_time_control,
        games=workload.games,
        llr=facts.llr if facts.sprt else None,
        llr_lower=facts.sprt.lower_llr if facts.sprt else None,
        llr_upper=facts.sprt.upper_llr if facts.sprt else None,
        elo=None if facts.mode == WorkloadMode.SPSA else elo_interval(facts.outcomes.primary()),
        created_at=workload.creation,
        updated_at=workload.updated,
        info=first_line(workload.info),
        diagnosis=diagnosis,
    )


def next_cursor(query: ListingQuery, workloads: list[Test]) -> int | None:
    walking = query.since_id is not None and len(workloads) == query.limit
    return workloads[-1].id if walking else None


def workloads_payload(query: ListingQuery, now: datetime | None = None) -> dict[str, Json]:

    workloads = listed_workloads(query)
    diagnoses = diagnose_workloads(workloads, now)
    rows = [
        workload_row(workload, DiagnosisRef(diagnoses[workload.id].state, diagnoses[workload.id].headline))
        for workload in workloads
    ]
    return {'workloads': to_json(rows), 'count': len(rows), 'next_since_id': next_cursor(query, workloads)}
