from django.http import HttpRequest, HttpResponse
from django.middleware.csrf import CsrfViewMiddleware
from django.shortcuts import render

import OpenBench.config
from OpenBench.models import EngineConfig


def fails_session_csrf(request: HttpRequest) -> bool:
    # For views exempt from CSRF so that credentialed Scripts can reach them.
    # A request riding a browser session must still carry a valid token
    if not request.user.is_authenticated:
        return False

    return (
        CsrfViewMiddleware(lambda _: None).process_view(request, None, (), {})
        is not None
    )


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    # Django's own failure page carries an inline <style> the CSP blocks
    context = {
        "reason": reason,
        "config": OpenBench.config.OPENBENCH_CONFIG,
        "static_version": OpenBench.config.OPENBENCH_STATIC_VERSION,
        "engines": EngineConfig.objects.filter(enabled=True).order_by("name"),
    }
    return render(request, "OpenBench/csrf_failure.html", context, status=403)
