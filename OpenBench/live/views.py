from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from OpenBench import upstream
from OpenBench.diagnosis.views import AUTHENTICATION_ERROR, parameters
from OpenBench.live.listing import listing_payload, live_listing
from OpenBench.live.token import MAX_TOKEN_LENGTH
from OpenBench.live.workload import find_workload, live_workload, workload_payload

MAX_AUTHOR_LENGTH = 150
LISTING_PARAMETERS = ('author', 'token')
WORKLOAD_PARAMETERS = ('token',)
AUTHOR_ERROR = f'author must be at most {MAX_AUTHOR_LENGTH} characters'
TOKEN_ERROR = f'token must be at most {MAX_TOKEN_LENGTH} characters'
MISSING_WORKLOAD_ERROR = 'Requested Workload Id does not exist'


def bad_request(message: str) -> HttpResponse:
    return upstream.api_response({'error': message}, status=400)


@csrf_exempt
def api_live_workloads(request: HttpRequest) -> HttpResponse:

    if not upstream.api_authenticate(request):
        return upstream.api_response({'error': AUTHENTICATION_ERROR}, status=401)

    query = parameters(request, LISTING_PARAMETERS)
    if len(query.get('author', '')) > MAX_AUTHOR_LENGTH:
        return bad_request(AUTHOR_ERROR)
    if len(query.get('token', '')) > MAX_TOKEN_LENGTH:
        return bad_request(TOKEN_ERROR)

    return upstream.api_response(listing_payload(live_listing(query.get('author'), query.get('token'))))


@csrf_exempt
def api_live_workload(request: HttpRequest, workload_id: int) -> HttpResponse:

    if not upstream.api_authenticate(request):
        return upstream.api_response({'error': AUTHENTICATION_ERROR}, status=401)

    query = parameters(request, WORKLOAD_PARAMETERS)
    if len(query.get('token', '')) > MAX_TOKEN_LENGTH:
        return bad_request(TOKEN_ERROR)

    if (workload := find_workload(workload_id)) is None:
        return upstream.api_response({'error': MISSING_WORKLOAD_ERROR}, status=404)

    return upstream.api_response(workload_payload(live_workload(workload, query.get('token'))))
