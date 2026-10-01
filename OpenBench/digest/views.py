from typing import cast

from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from OpenBench import upstream
from OpenBench.digest.domain import DEFAULT_CHOICE, DigestReport, WindowChoice
from OpenBench.digest.present import digest_page
from OpenBench.digest.report import digest_report
from OpenBench.digest.serialize import digest_json
from OpenBench.digest.window import BadWindow, parse_choice, resolve
from OpenBench.insights.serialize import to_json
from OpenBench.progress.views import viewer_refused

TEMPLATE = 'digest.html'
REPORT_CACHE_SECONDS = 60
AUTHENTICATION_ERROR = 'API requires authentication for this server'


def report_for(choice: WindowChoice) -> DigestReport:
    window = resolve(choice, timezone.now())
    # A timestamp window starts on a whole minute of the last 30 days, which bounds the keys
    key = f'digest:{window.preset.value if window.preset else window.since.isoformat()}'
    return cast(DigestReport, cache.get_or_set(key, lambda: digest_report(window), REPORT_CACHE_SECONDS))


def page_report(request: HttpRequest) -> DigestReport:
    try:
        return report_for(parse_choice(request.GET.get('since'), request.GET.get('from')))
    except BadWindow:
        return report_for(DEFAULT_CHOICE)


def digest(request: HttpRequest) -> HttpResponse:
    if viewer_refused(request):
        return upstream.render(request, TEMPLATE)

    report = page_report(request)
    return upstream.render(request, TEMPLATE, {'page': digest_page(report), 'payload': to_json(report.fleet)})


def parameter(request: HttpRequest, name: str) -> str | None:
    # Scripts POST their credentials, so the window may arrive in the query string or in the body
    return request.GET.get(name) or request.POST.get(name)


@csrf_exempt
def api_digest(request: HttpRequest) -> HttpResponse:
    if not upstream.api_authenticate(request):
        return upstream.api_response({'error': AUTHENTICATION_ERROR}, status=401)

    try:
        report = report_for(parse_choice(parameter(request, 'since'), parameter(request, 'from')))
    except BadWindow as error:
        return upstream.api_response({'error': str(error)}, status=400)

    return upstream.api_response({'digest': digest_json(report)})
