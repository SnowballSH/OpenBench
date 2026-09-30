from django.http import HttpRequest, HttpResponse, HttpResponseServerError
from django.shortcuts import render
from django.template.loader import render_to_string

import OpenBench.config
from OpenBench import upstream


def layout_context() -> upstream.Context:
    # No Engines for the sidebar: an error page runs no query, and an
    # anonymous visitor to a missing page learns nothing about the server
    return {
        'config': upstream.openbench_config(),
        'static_version': OpenBench.config.OPENBENCH_STATIC_VERSION,
        'engines': (),
    }


def page_not_found(request: HttpRequest, exception: Exception) -> HttpResponse:
    return render(request, 'OpenBench/404.html', layout_context(), status=404)


def permission_denied(request: HttpRequest, exception: Exception) -> HttpResponse:
    return render(request, 'OpenBench/403.html', layout_context(), status=403)


def server_error(request: HttpRequest) -> HttpResponse:
    # Standalone: the database or the session may be what failed
    context = {'static_version': OpenBench.config.OPENBENCH_STATIC_VERSION}
    return HttpResponseServerError(render_to_string('OpenBench/500.html', context))
