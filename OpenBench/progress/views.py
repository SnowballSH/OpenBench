from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

import OpenBench.views
from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.insights.serialize import to_json
from OpenBench.models import EngineConfig
from OpenBench.progress.analysis import parse_engine, parse_window
from OpenBench.progress.domain import DEFAULT_WINDOW, Window
from OpenBench.progress.present import progress_page, progress_url
from OpenBench.progress.report import progress_report

TEMPLATE = "progress.html"
WINDOW_ERROR = f"window must be one of {', '.join(window.value for window in Window)}"


def query_text(request: HttpRequest, key: str) -> str | None:
    value = request.GET.get(key)
    return value if isinstance(value, str) else None


def viewer_refused(request: HttpRequest) -> bool:
    return (
        OPENBENCH_CONFIG["require_login_to_view"] and not request.user.is_authenticated
    )


def progress(request: HttpRequest, engine: str | None = None) -> HttpResponse:
    if viewer_refused(request):
        return OpenBench.views.render(request, TEMPLATE)

    window = parse_window(query_text(request, "window")) or DEFAULT_WINDOW

    if engine is None and (chosen := parse_engine(query_text(request, "engine"))):
        return OpenBench.views.redirect(request, progress_url(chosen, window))

    report = progress_report(window, parse_engine(engine))
    configured = EngineConfig.objects.filter(enabled=True).values_list(
        "name", flat=True
    )
    return OpenBench.views.render(
        request,
        TEMPLATE,
        {"page": progress_page(report, configured), "payload": to_json(report)},
    )


@csrf_exempt
def api_progress(request: HttpRequest) -> HttpResponse:
    if not OpenBench.views.api_authenticate(request):
        return OpenBench.views.api_response(
            {"error": "API requires authentication for this server"}, status=401
        )

    if (window := parse_window(query_text(request, "window"))) is None:
        return OpenBench.views.api_response({"error": WINDOW_ERROR}, status=400)

    report = progress_report(window, parse_engine(query_text(request, "engine")))
    return OpenBench.views.api_response({"progress": to_json(report)})
