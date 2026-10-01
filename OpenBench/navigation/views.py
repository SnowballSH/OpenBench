from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_safe

from OpenBench import upstream
from OpenBench.navigation.catalogue import DatabaseCatalogue
from OpenBench.navigation.resolve import resolve
from OpenBench.navigation.suggest import suggestions, to_json

QUERY_PARAMETER = 'q'
AUTHENTICATION_ERROR = 'API requires authentication for this server'


@require_safe
def go(request: HttpRequest) -> HttpResponse:
    jump = resolve(request.GET.get(QUERY_PARAMETER, ''), DatabaseCatalogue())
    return upstream.redirect(request, jump.path, error=jump.notice)


@csrf_exempt
def api_jump(request: HttpRequest) -> HttpResponse:
    if not upstream.api_authenticate(request):
        return upstream.api_response({'error': AUTHENTICATION_ERROR}, status=401)

    found = suggestions(request.GET.get(QUERY_PARAMETER, ''), DatabaseCatalogue())
    return upstream.api_response({'suggestions': to_json(found)})
