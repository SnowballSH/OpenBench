from datetime import datetime

from OpenBench.digest.domain import ERRORS_SENT, DigestWindow, ErrorDigest, ErrorLine
from OpenBench.triage.groups import error_events, group_rows, with_affected
from OpenBench.triage.query import ErrorQuery

EVERY_ERROR = ErrorQuery()


def error_digest(window: DigestWindow, now: datetime) -> ErrorDigest:
    rows, truncated = group_rows(EVERY_ERROR, now)
    seen = sorted((row for row in rows if window.holds(row.group.last_seen)), key=lambda row: row.verdict.resolved)
    shown = with_affected(seen[:ERRORS_SENT], error_events(EVERY_ERROR))
    return ErrorDigest(
        total=len(seen),
        new=sum(row.group.first_seen >= window.since for row in seen),
        unresolved=sum(not row.verdict.resolved for row in seen),
        lines=[ErrorLine(row, row.group.first_seen >= window.since) for row in shown],
        omitted=max(0, len(seen) - ERRORS_SENT),
        truncated=truncated,
    )
