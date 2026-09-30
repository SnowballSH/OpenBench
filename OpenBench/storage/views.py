from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from OpenBench import upstream
from OpenBench.models import Profile
from OpenBench.storage.present import storage_page
from OpenBench.storage.report import current_report
from OpenBench.storage.serialize import report_json

LOGIN_REQUIRED = 'Storage usage requires a login'
MANAGERS_ONLY = 'Storage usage is visible to managers only'


def is_manager(user: User) -> bool:
    return user.is_superuser or Profile.objects.filter(user=user, enabled=True, superuser=True).exists()


def manage_storage(request: HttpRequest) -> HttpResponse:
    if not request.user.is_authenticated:
        return upstream.redirect(request, '/login/', error=LOGIN_REQUIRED)

    if not Profile.objects.filter(user=request.user, enabled=True).exists():
        return upstream.redirect(request, '/index/', error=upstream.error_message('disabled'))

    if not is_manager(request.user):
        return upstream.redirect(request, '/manage/books/', error=MANAGERS_ONLY)

    return upstream.render(
        request, 'manage_storage.html', {'storage': storage_page(current_report()), 'can_manage': True}
    )


@csrf_exempt
def api_storage(request: HttpRequest) -> HttpResponse:
    if not (user := upstream.api_user(request)):
        return upstream.api_response({'error': 'API requires authentication for this endpoint'}, status=401)

    if not is_manager(user):
        return upstream.api_response({'error': MANAGERS_ONLY}, status=403)

    return upstream.api_response({'storage': report_json(current_report())})
