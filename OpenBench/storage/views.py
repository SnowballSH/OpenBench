from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

import OpenBench.views
from OpenBench.storage.present import storage_page
from OpenBench.storage.report import current_report
from OpenBench.storage.serialize import report_json

LOGIN_REQUIRED = "Storage usage requires a login"


def manage_storage(request: HttpRequest) -> HttpResponse:
    if not request.user.is_authenticated:
        return OpenBench.views.redirect(request, "/login/", error=LOGIN_REQUIRED)

    return OpenBench.views.render(
        request, "manage_storage.html", {"storage": storage_page(current_report())}
    )


@csrf_exempt
def api_storage(request: HttpRequest) -> HttpResponse:
    if not OpenBench.views.api_authenticate(request, require_enabled=True):
        return OpenBench.views.api_response(
            {"error": "API requires authentication for this endpoint"}, status=401
        )

    return OpenBench.views.api_response({"storage": report_json(current_report())})
