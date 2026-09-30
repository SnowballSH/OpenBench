from collections.abc import Callable, Mapping, Sequence

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.urls import reverse

type Policy = Mapping[str, Sequence[str]]

HEADER = "Content-Security-Policy"


def serialize_policy(policy: Policy) -> str:
    return "; ".join(
        " ".join((directive, *sources)) for directive, sources in policy.items()
    )


def policy_for(path: str) -> Policy:
    if path.startswith(reverse("admin:index")):
        return settings.OPENBENCH_CSP_ADMIN
    return settings.OPENBENCH_CSP


class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        response.headers.setdefault(HEADER, serialize_policy(policy_for(request.path)))
        return response
