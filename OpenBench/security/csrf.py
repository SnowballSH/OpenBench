from django.http import HttpRequest
from django.middleware.csrf import CsrfViewMiddleware


def fails_session_csrf(request: HttpRequest) -> bool:
    # For views exempt from CSRF so that credentialed Scripts can reach them.
    # A request riding a browser session must still carry a valid token
    if not request.user.is_authenticated:
        return False

    return (
        CsrfViewMiddleware(lambda _: None).process_view(request, None, (), {})
        is not None
    )
