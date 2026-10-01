from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from OpenBench import upstream
from OpenBench.games.report import game_report
from OpenBench.games.service import VIEW_BUDGET, archive_size, refresh
from OpenBench.insights.serialize import Json, to_json
from OpenBench.models import Test

AUTHENTICATION_ERROR = 'API requires authentication for this server'
UPLOADS_OFF = 'FALSE'


def games_payload(test: Test) -> Json:

    facts: dict[str, Json] = {'upload_pgns': test.upload_pgns, 'active': not test.finished}

    if test.upload_pgns == UPLOADS_OFF:
        return {'status': 'disabled', **facts, 'report': None}

    row = refresh(test, VIEW_BUDGET)
    if row is None or not row.members:
        return {'status': 'empty', **facts, 'report': None}

    report = game_report(row, archive_size(test.id) or row.analysed_bytes)
    return {'status': 'ready', **facts, 'report': to_json(report)}


@csrf_exempt
def api_workload_games(request: HttpRequest, workload_id: int) -> HttpResponse:

    if not upstream.api_authenticate(request):
        return upstream.api_response({'error': AUTHENTICATION_ERROR}, status=401)

    if (test := Test.objects.filter(id=workload_id).first()) is None:
        return upstream.api_response({'error': 'Requested Workload Id does not exist'}, status=404)

    return upstream.api_response({'games': games_payload(test)})
