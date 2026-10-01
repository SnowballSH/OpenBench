from collections.abc import Iterable

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from OpenBench import upstream
from OpenBench.diagnosis.api import BadQuery, parse_query, workloads_payload

AUTHENTICATION_ERROR = 'API requires authentication for this server'
PARAMETERS = ('status', 'engine', 'since_id', 'limit')


def parameters(request: HttpRequest, names: Iterable[str] = PARAMETERS) -> dict[str, str]:
    # Scripts POST their credentials, so a filter may arrive in the query string or in the body
    return {name: value for name in names if (value := request.GET.get(name) or request.POST.get(name))}


@csrf_exempt
def api_workloads(request: HttpRequest) -> HttpResponse:

    if not upstream.api_authenticate(request):
        return upstream.api_response({'error': AUTHENTICATION_ERROR}, status=401)

    try:
        query = parse_query(parameters(request))
    except BadQuery as error:
        return upstream.api_response({'error': str(error)}, status=400)

    return upstream.api_response(workloads_payload(query))
