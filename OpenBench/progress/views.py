import hashlib
from typing import cast

from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from OpenBench import upstream
from OpenBench.insights.serialize import to_json
from OpenBench.models import EngineConfig
from OpenBench.progress.analysis import parse_engine, parse_window
from OpenBench.progress.domain import DEFAULT_WINDOW, ProgressReport, Window
from OpenBench.progress.present import path_safe, progress_page, progress_url
from OpenBench.progress.report import progress_report

TEMPLATE = 'progress.html'
REPORT_CACHE_SECONDS = 60
WINDOW_ERROR = f'window must be one of {", ".join(window.value for window in Window)}'


def viewer_refused(request: HttpRequest) -> bool:
    return upstream.openbench_config()['require_login_to_view'] and not request.user.is_authenticated


def cached_report(window: Window, engine: str | None) -> ProgressReport:
    digest = hashlib.sha256((engine or '').encode()).hexdigest()
    key = f'progress:{window.value}:{digest}'
    return cast(ProgressReport, cache.get_or_set(key, lambda: progress_report(window, engine), REPORT_CACHE_SECONDS))


def progress(request: HttpRequest, engine: str | None = None) -> HttpResponse:
    if viewer_refused(request):
        return upstream.render(request, TEMPLATE)

    window = parse_window(request.GET.get('window')) or DEFAULT_WINDOW
    queried = parse_engine(request.GET.get('engine'))
    chosen = parse_engine(engine) if engine is not None else queried

    if engine is not None and chosen is None:
        return upstream.redirect(request, progress_url(None, window))

    if engine is None and chosen is not None and path_safe(chosen):
        return upstream.redirect(request, progress_url(chosen, window))

    report = cached_report(window, chosen)
    configured = EngineConfig.objects.filter(enabled=True).values_list('name', flat=True)
    return upstream.render(
        request,
        TEMPLATE,
        {'page': progress_page(report, configured), 'payload': to_json(report)},
    )


@csrf_exempt
def api_progress(request: HttpRequest) -> HttpResponse:
    if not upstream.api_authenticate(request):
        return upstream.api_response({'error': 'API requires authentication for this server'}, status=401)

    if (window := parse_window(request.GET.get('window'))) is None:
        return upstream.api_response({'error': WINDOW_ERROR}, status=400)

    report = cached_report(window, parse_engine(request.GET.get('engine')))
    return upstream.api_response({'progress': to_json(report)})
