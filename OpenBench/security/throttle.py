import hashlib

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest

FAILURE_LIMIT  = 10
WINDOW_SECONDS = 15 * 60

def client_ip(request: HttpRequest) -> str:

    # Caddy appends the peer address it saw, so only the right-most hop is trustworthy
    if settings.OPENBENCH_BEHIND_TLS_PROXY:
        hops = [hop.strip() for hop in request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')]
        if hops[-1]:
            return hops[-1]

    return request.META.get('REMOTE_ADDR', '')

def _cache_key(kind: str, value: str) -> str:
    return 'auth-failures:%s:%s' % (kind, hashlib.sha256(value.encode()).hexdigest())

def _failure_keys(request: HttpRequest, username: str) -> tuple[str, str]:
    return (_cache_key('user', username.casefold()), _cache_key('ip', client_ip(request)))

def is_throttled(request: HttpRequest, username: str) -> bool:
    return any(cache.get(key, 0) >= FAILURE_LIMIT for key in _failure_keys(request, username))

def record_failure(request: HttpRequest, username: str) -> None:

    # The window is fixed from the first failure; incr() leaves the expiry untouched
    for key in _failure_keys(request, username):
        cache.add(key, 0, WINDOW_SECONDS)
        try: cache.incr(key)
        except ValueError: cache.set(key, 1, WINDOW_SECONDS)
