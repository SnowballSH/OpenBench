from typing import Any, cast

from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse

import OpenBench.config
import OpenBench.views

type Context = dict[str, Any]


def openbench_config() -> Context:
    # OpenBench/apps.py fills the config in before any request or command runs
    return cast(Context, OpenBench.config.OPENBENCH_CONFIG)


def error_message(key: str) -> str:
    message: str = OpenBench.views.ERROR_MESSAGES[key]
    return message


def render(request: HttpRequest, template: str, content: Context | None = None) -> HttpResponse:
    response: HttpResponse = OpenBench.views.render(request, template, content or {})
    return response


def redirect(request: HttpRequest, destination: str, *, error: str | None = None) -> HttpResponse:
    response: HttpResponse = OpenBench.views.redirect(request, destination, error=error)
    return response


def api_response(data: Context, status: int = 200) -> HttpResponse:
    response: HttpResponse = OpenBench.views.api_response(data, status=status)
    return response


def api_user(request: HttpRequest) -> User | None:
    user: User | None = OpenBench.views.api_user(request)
    return user


def api_authenticate(request: HttpRequest) -> bool:
    authenticated: bool = OpenBench.views.api_authenticate(request)
    return authenticated
