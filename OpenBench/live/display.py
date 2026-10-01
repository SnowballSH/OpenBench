from typing import cast

from OpenBench.diagnosis.domain import Diagnosis
from OpenBench.insights.domain import WorkloadStatus
from OpenBench.insights.listing import RowTiming
from OpenBench.insights.sources import workload_facts
from OpenBench.listing_rows import result_label
from OpenBench.live.domain import LiveProgress, LiveReason, LiveResult, LiveTiming
from OpenBench.models import Test
from OpenBench.templatetags import mytags


def result_colour(test: Test) -> str:
    return cast(str, mytags.testResultColour(test))


def stat_lines(test: Test, *, detailed: bool) -> list[str]:
    # The workload page shows the long block, except for a tune, which has none
    block = mytags.longStatBlock(test) if detailed and test.test_mode != 'SPSA' else mytags.shortStatBlock(test)
    return cast(str, block).split('\n')


def live_result(test: Test, *, detailed: bool) -> LiveResult:
    colour = result_colour(test)
    return LiveResult(
        status=workload_facts(test).status,
        games=test.games,
        colour=colour,
        outcome=result_label(test, colour),
        statblock=stat_lines(test, detailed=detailed),
    )


def live_progress(test: Test, status: WorkloadStatus) -> LiveProgress | None:
    # Only an active row carries the bar, as in the index template
    progress = mytags.workload_progress(test) if status == WorkloadStatus.ACTIVE else None
    return LiveProgress(progress.kind, progress.fraction, progress.label) if progress else None


def live_timing(timing: RowTiming | None) -> LiveTiming | None:
    if timing is None:
        return None
    return LiveTiming(timing.kind, timing.text, timing.estimate, timing.note, timing.rate, timing.rate_window)


def live_reason(reason: Diagnosis | None) -> LiveReason | None:
    return LiveReason(reason.severity, reason.headline, reason.brief) if reason else None
