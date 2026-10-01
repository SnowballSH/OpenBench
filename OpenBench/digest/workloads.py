from collections.abc import Mapping, Sequence
from datetime import datetime

from OpenBench import upstream
from OpenBench.diagnosis.domain import Diagnosis
from OpenBench.digest.domain import (
    RUNNING_SENT,
    DiagnosisBrief,
    DigestWindow,
    FinishedCounts,
    FinishedDigest,
    FinishedWorkload,
    LlrProgress,
    RunningDigest,
    RunningWorkload,
    WorkloadRef,
)
from OpenBench.insights.domain import WorkloadFacts, WorkloadMode, WorkloadStatus
from OpenBench.insights.eta import Eta, timing_and_eta
from OpenBench.insights.sources import workload_facts
from OpenBench.insights.strength import EloInterval, elo_interval
from OpenBench.insights.timing import timeline_marks
from OpenBench.models import Test
from OpenBench.page_queries import ListedTest, annotated_snapshots
from OpenBench.progress.conditions import time_class

MATCH_MODES = (WorkloadMode.SPRT, WorkloadMode.GAMES)

type Diagnoses = Mapping[int, Diagnosis]


def workload_ref(test: Test) -> WorkloadRef:
    label = upstream.workload_label(test)
    return WorkloadRef(
        id=test.id,
        url=upstream.workload_url(test),
        title=label.title,
        commits=label.commits,
        author=test.author,
        engine=test.dev_engine,
        mode=WorkloadMode(test.test_mode),
        time_control=test.dev_time_control,
        time_class=time_class(test.dev_time_control, test.base_time_control, test.dev_options, test.base_options),
    )


def match_elo(facts: WorkloadFacts) -> EloInterval | None:
    return elo_interval(facts.outcomes.primary()) if facts.mode in MATCH_MODES else None


def started_at(test: ListedTest) -> datetime:
    return test.first_snapshot_at or test.creation


def stop_reason(diagnosis: Diagnosis | None) -> str | None:
    return diagnosis.brief if diagnosis and diagnosis.shown else None


def finished_status(test: Test, facts: WorkloadFacts) -> WorkloadStatus:
    if facts.mode != WorkloadMode.SPRT:
        return WorkloadStatus.COMPLETED
    flags = ((test.passed, WorkloadStatus.PASSED), (test.failed, WorkloadStatus.FAILED))
    return next((status for flag, status in flags if flag), WorkloadStatus.STOPPED)


def unfinished_status(test: Test) -> WorkloadStatus:
    return WorkloadStatus.ACTIVE if test.approved else WorkloadStatus.PENDING


def finished_workload(test: ListedTest, diagnoses: Diagnoses) -> FinishedWorkload:
    facts = workload_facts(test)
    started = started_at(test)
    return FinishedWorkload(
        workload=workload_ref(test),
        status=finished_status(test, facts),
        elo=match_elo(facts),
        games=test.games,
        started_at=started,
        finished_at=test.finished_at,
        duration_seconds=max(0.0, (test.finished_at - started).total_seconds()),
        stop_reason=stop_reason(diagnoses.get(test.id)),
    )


def finished_digest(counts: FinishedCounts, tests: Sequence[ListedTest], diagnoses: Diagnoses) -> FinishedDigest:
    return FinishedDigest(
        counts=counts,
        workloads=[finished_workload(test, diagnoses) for test in tests],
        omitted=max(0, counts.total - len(tests)),
    )


def rate_and_eta(test: ListedTest, facts: WorkloadFacts, now: datetime) -> tuple[float | None, Eta | None]:
    if not test.approved:
        return None, None
    current = (facts.updated_at, facts.outcomes.games)
    marks = timeline_marks(facts.created_at, current, annotated_snapshots(test).marks())
    timing, eta = timing_and_eta(facts, marks, now)
    rate = timing.best_rate() if timing else None
    return (rate.games_per_hour if rate and rate.games_per_hour > 0 else None), eta


def diagnosis_brief(diagnosis: Diagnosis) -> DiagnosisBrief:
    return DiagnosisBrief(diagnosis.state, diagnosis.severity, diagnosis.headline, diagnosis.brief)


def running_workload(test: ListedTest, window: DigestWindow, diagnoses: Diagnoses) -> RunningWorkload:
    facts = workload_facts(test)
    started = started_at(test)
    rate, eta = rate_and_eta(test, facts, window.until)
    return RunningWorkload(
        workload=workload_ref(test),
        status=unfinished_status(test),
        started_in_window=window.holds(started),
        started_at=started,
        games=test.games,
        elo=match_elo(facts),
        llr=LlrProgress(facts.llr, facts.sprt.lower_llr, facts.sprt.upper_llr) if facts.sprt else None,
        games_per_hour=rate,
        eta=eta,
        diagnosis=diagnosis_brief(diagnoses[test.id]),
    )


def running_digest(tests: Sequence[ListedTest], window: DigestWindow, diagnoses: Diagnoses) -> RunningDigest:
    rows = [running_workload(test, window, diagnoses) for test in tests]
    newest_first = sorted(rows, key=lambda row: (row.started_at, row.workload.id), reverse=True)
    return RunningDigest(
        total=len(rows),
        pending=sum(row.status == WorkloadStatus.PENDING for row in rows),
        started=sum(row.started_in_window and row.status == WorkloadStatus.ACTIVE for row in rows),
        workloads=newest_first[:RUNNING_SENT],
        omitted=max(0, len(rows) - RUNNING_SENT),
    )
