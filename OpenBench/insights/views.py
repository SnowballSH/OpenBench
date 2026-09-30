from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from OpenBench import upstream
from OpenBench.insights.api import server_payload
from OpenBench.insights.export import history_csv, history_filename
from OpenBench.insights.workload import workload_history
from OpenBench.models import Test

AUTHENTICATION_ERROR = 'API requires authentication for this server'


@csrf_exempt
def api_server_insights(request: HttpRequest) -> HttpResponse:

    if not upstream.api_authenticate(request):
        return upstream.api_response({'error': AUTHENTICATION_ERROR}, status=401)

    return upstream.api_response({'server': server_payload()})


@csrf_exempt
def api_workload_history_csv(request: HttpRequest, workload_id: str) -> HttpResponse:

    if not upstream.api_authenticate(request):
        return upstream.api_response({'error': AUTHENTICATION_ERROR}, status=401)

    if (test := Test.objects.select_related('spsa_run').filter(id=int(workload_id)).first()) is None:
        return upstream.api_response({'error': 'Requested Workload Id does not exist'}, status=404)

    response = HttpResponse(history_csv(workload_history(test).points), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{history_filename(test.id)}"'
    return response
