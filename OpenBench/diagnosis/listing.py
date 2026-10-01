from collections.abc import Sequence
from datetime import datetime

from OpenBench.diagnosis.domain import Diagnosis
from OpenBench.diagnosis.report import diagnose_active_workloads, diagnose_workloads, is_stopped
from OpenBench.models import Test

ROW_REASON = 'row_reason'


def attach_row_reasons(tests: Sequence[Test], now: datetime) -> None:

    if not tests:
        return

    diagnoses = diagnose_active_workloads(now)
    for test in tests:
        setattr(test, ROW_REASON, diagnoses.get(test.id))


def attach_stop_reasons(tests: Sequence[Test], now: datetime) -> None:

    stopped = [test for test in tests if is_stopped(test)]
    for test, diagnosis in zip(stopped, diagnose_workloads(stopped, now).values(), strict=True):
        setattr(test, ROW_REASON, diagnosis if diagnosis.shown else None)


def row_reason(test: Test) -> Diagnosis | None:
    reason: Diagnosis | None = getattr(test, ROW_REASON, None)
    return reason
