from django.contrib.auth.models import AbstractBaseUser
from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

import OpenBench.views
from OpenBench.models import Profile
from OpenBench.storage.present import storage_page
from OpenBench.storage.report import current_report
from OpenBench.storage.serialize import report_json

LOGIN_REQUIRED = 'Storage usage requires a login'
MANAGERS_ONLY = 'Storage usage is visible to managers only'


def is_manager(user: AbstractBaseUser) -> bool:
    return user.is_superuser or Profile.objects.filter(user=user, enabled=True, superuser=True).exists()


def manage_storage(request: HttpRequest) -> HttpResponse:
    if not request.user.is_authenticated:
        return OpenBench.views.redirect(request, '/login/', error=LOGIN_REQUIRED)

    if not Profile.objects.filter(user=request.user, enabled=True).exists():
        return OpenBench.views.redirect(request, '/index/', error=OpenBench.views.ERROR_MESSAGES['disabled'])

    if not is_manager(request.user):
        return OpenBench.views.redirect(request, '/manage/books/', error=MANAGERS_ONLY)

    return OpenBench.views.render(
        request, 'manage_storage.html', {'storage': storage_page(current_report()), 'can_manage': True}
    )


@csrf_exempt
def api_storage(request: HttpRequest) -> HttpResponse:
    if not (user := OpenBench.views.api_user(request)):
        return OpenBench.views.api_response({'error': 'API requires authentication for this endpoint'}, status=401)

    if not is_manager(user):
        return OpenBench.views.api_response({'error': MANAGERS_ONLY}, status=403)

    return OpenBench.views.api_response({'storage': report_json(current_report())})
