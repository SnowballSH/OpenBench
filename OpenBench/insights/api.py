import OpenBench.insights.server
import OpenBench.insights.workload

from OpenBench.insights.serialize import Json, to_json
from OpenBench.models import Test

def workload_payload(test: Test) -> Json:
    return to_json(OpenBench.insights.workload.workload_insights(test))

def server_payload() -> Json:
    return to_json(OpenBench.insights.server.server_insights())
