from collections.abc import Callable

from django.http import HttpRequest, HttpResponse, JsonResponse

from OpenBench.security.throttle import LoginThrottled

class LoginThrottleMiddleware:

    # API views let LoginThrottled propagate, so every one of them answers a
    # throttled credential check with the same distinguishable 429

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        return self.get_response(request)

    def process_exception(self, request: HttpRequest, exception: Exception) -> HttpResponse | None:
        if isinstance(exception, LoginThrottled):
            return JsonResponse({ 'error' : 'Too many failed logins' }, status=429)
        return None
