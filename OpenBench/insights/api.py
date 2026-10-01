import OpenBench.insights.server
import OpenBench.insights.workload
from OpenBench.diagnosis.report import diagnose_workload
from OpenBench.insights.serialize import Json, to_json
from OpenBench.models import Test


def workload_payload(test: Test) -> Json:
    insights = OpenBench.insights.workload.workload_insights(test)
    return {**to_json_object(insights), 'diagnosis': to_json(diagnose_workload(test, insights.generated_at))}


def to_json_object(value: object) -> dict[str, Json]:
    payload = to_json(value)
    if not isinstance(payload, dict):
        raise TypeError(f'Expected an object, got {payload!r}')
    return payload


def server_payload() -> Json:
    return to_json(OpenBench.insights.server.server_insights())
