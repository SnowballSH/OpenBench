from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.base_user import AbstractBaseUser
from django.http import HttpRequest

from OpenBench.security import throttle

class ThrottledModelBackend(ModelBackend):

    # Every password check that carries its request is throttled here, which
    # covers the site, the Client, the API, and the Django admin login alike

    def authenticate(self, request: HttpRequest | None, username: str | None = None,
                     password: str | None = None, **kwargs) -> AbstractBaseUser | None:

        if request is None or not username:
            return super().authenticate(request, username, password, **kwargs)

        if throttle.is_throttled(request, username):
            return None

        if (user := super().authenticate(request, username, password, **kwargs)) is None:
            throttle.record_failure(request, username)

        return user
