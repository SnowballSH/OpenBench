import hashlib

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest

WINDOW_SECONDS = 15 * 60
ACCOUNT_LIMIT = 10
ADDRESS_LIMIT = 50


class LoginThrottled(Exception):
    pass


def client_ip(request: HttpRequest) -> str:

    # Caddy appends the peer address it saw, so only the right-most hop is trustworthy
    if settings.OPENBENCH_BEHIND_TLS_PROXY:
        hops = [hop.strip() for hop in request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')]
        if hops[-1]:
            return hops[-1]

    return request.META.get('REMOTE_ADDR', '')


def _cache_key(kind: str, *parts: str) -> str:
    digest = hashlib.sha256('\0'.join(parts).encode()).hexdigest()
    return f'auth-failures:{kind}:{digest}'


def _limited_keys(request: HttpRequest, username: str) -> tuple[tuple[str, int], ...]:

    # No key is the username alone, so failures elsewhere never lock out a correct login here
    address = client_ip(request)
    return (
        (_cache_key('account', username.casefold(), address), ACCOUNT_LIMIT),
        (_cache_key('address', address), ADDRESS_LIMIT),
    )


def is_throttled(request: HttpRequest, username: str) -> bool:
    return any(cache.get(key, 0) >= limit for key, limit in _limited_keys(request, username))


def record_failure(request: HttpRequest, username: str) -> None:

    # The window is fixed from the first failure; incr() leaves the expiry untouched
    for key, _ in _limited_keys(request, username):
        cache.add(key, 0, WINDOW_SECONDS)
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, WINDOW_SECONDS)
