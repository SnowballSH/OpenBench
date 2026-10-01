from collections.abc import Callable

from django.http import HttpRequest, HttpResponse, HttpResponseRedirect

from OpenBench import upstream
from OpenBench.security.robots import ROBOTS_URL

LOGIN_URL = '/login/'
PAGE_METHODS = frozenset({'GET', 'HEAD'})

# Each of these authenticates by itself, or must stay reachable without a login
PUBLIC_PREFIXES = (
    LOGIN_URL,
    '/register/',
    '/logout/',
    '/health/',
    ROBOTS_URL,
    '/static/',
    '/admin/',
    '/scripts/',
    '/api/',
    '/client',
)


def needs_login(request: HttpRequest) -> bool:
    return (
        request.method in PAGE_METHODS
        and upstream.openbench_config()['require_login_to_view']
        and not request.path_info.startswith(PUBLIC_PREFIXES)
        and not request.user.is_authenticated
    )


class LoginRequiredMiddleware:
    # Sends anonymous page views to the login page before the view queries
    # anything, and without writing a flash message into a new session

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if needs_login(request):
            return HttpResponseRedirect(LOGIN_URL)
        return self.get_response(request)
