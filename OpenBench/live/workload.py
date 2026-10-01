from datetime import datetime

from django.utils import timezone

from OpenBench.diagnosis.report import diagnose_workload
from OpenBench.insights.serialize import Json, to_json
from OpenBench.live.display import live_result
from OpenBench.live.domain import LiveWorkload, Unchanged
from OpenBench.live.token import workload_token
from OpenBench.models import Test


def find_workload(workload_id: int) -> Test | None:
    return Test.objects.select_related('dev', 'base', 'spsa_run').filter(id=workload_id).first()


def live_workload(workload: Test, known_token: str | None, now: datetime | None = None) -> LiveWorkload | Unchanged:

    now = now or timezone.now()
    token = workload_token(workload, now)
    if token == known_token:
        return Unchanged(token)

    diagnosis = diagnose_workload(workload, now)
    return LiveWorkload(
        token=token,
        id=workload.id,
        result=live_result(workload, detailed=True),
        diagnosis=diagnosis if diagnosis.shown else None,
    )


def workload_payload(workload: LiveWorkload | Unchanged) -> dict[str, Json]:
    if isinstance(workload, Unchanged):
        return {'token': workload.token, 'changed': False}
    return {
        'token': workload.token,
        'changed': True,
        'workload': {
            'id': workload.id,
            'result': to_json(workload.result),
            'diagnosis': to_json(workload.diagnosis),
        },
    }
