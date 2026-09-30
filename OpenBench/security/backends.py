from typing import Any

from django.contrib.auth.backends import BaseBackend, ModelBackend
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.signals import user_login_failed
from django.core.exceptions import PermissionDenied
from django.dispatch import receiver
from django.http import HttpRequest

from OpenBench.security import throttle


class LoginThrottleBackend(BaseBackend):
    # Listed ahead of ModelBackend, which still checks every password, so the
    # backend path stored in existing sessions stays valid. PermissionDenied
    # stops Django from trying the remaining backends

    def authenticate(
        self, request: HttpRequest | None, username: str | None = None, password: str | None = None, **kwargs: Any
    ) -> None:

        if request is not None and username and throttle.is_throttled(request, username):
            raise PermissionDenied()

        return None

    def get_user(self, user_id: Any) -> AbstractBaseUser | None:

        # Being first, Django stores this path when login() is given no backend
        return ModelBackend().get_user(user_id)


# Django imports every backend before it can send user_login_failed, so this
# receiver is always connected in time
@receiver(user_login_failed, dispatch_uid='openbench-login-throttle')
def record_login_failure(
    sender: str, credentials: dict[str, Any], request: HttpRequest | None = None, **kwargs: Any
) -> None:

    # Refused attempts are not counted again, so a locked account on one
    # address never goes on to lock that whole address
    username = credentials.get('username')
    if request is not None and username and not throttle.is_throttled(request, username):
        throttle.record_failure(request, username)
