from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

import OpenBench.views
from OpenBench.insights.api import server_payload


@csrf_exempt
def api_server_insights(request: HttpRequest) -> HttpResponse:

    if not OpenBench.views.api_authenticate(request):
        return OpenBench.views.api_response({'error': 'API requires authentication for this server'}, status=401)

    return OpenBench.views.api_response({'server': server_payload()})
