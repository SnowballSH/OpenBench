from collections.abc import Sequence
from datetime import datetime

from OpenBench.diagnosis.domain import Diagnosis
from OpenBench.diagnosis.report import diagnose_active_workloads
from OpenBench.models import Test

ROW_REASON = 'row_reason'


def attach_row_reasons(tests: Sequence[Test], now: datetime) -> None:

    if not tests:
        return

    diagnoses = diagnose_active_workloads(now)
    for test in tests:
        setattr(test, ROW_REASON, diagnoses.get(test.id))


def row_reason(test: Test) -> Diagnosis | None:
    reason: Diagnosis | None = getattr(test, ROW_REASON, None)
    return reason
