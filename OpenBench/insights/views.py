from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from OpenBench import upstream
from OpenBench.insights.api import server_payload


@csrf_exempt
def api_server_insights(request: HttpRequest) -> HttpResponse:

    if not upstream.api_authenticate(request):
        return upstream.api_response({'error': 'API requires authentication for this server'}, status=401)

    return upstream.api_response({'server': server_payload()})
