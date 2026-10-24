"""Password login with a signed, long-lived cookie. The app is reached privately (Tailscale), so one shared password is enough."""

import hashlib
import hmac
import time

from ..config import Settings

COOKIE = "clipper_session"
MAX_AGE_S = 90 * 24 * 3600


class Auth:
    def __init__(self, settings: Settings):
        self.password = settings.app_password
        secret = settings.secret_key or hashlib.sha256(f"stream-clipper:{self.password}".encode()).hexdigest()
        self._key = secret.encode()

    @property
    def required(self) -> bool:
        return bool(self.password)

    def check_password(self, password: str) -> bool:
        return bool(self.password) and hmac.compare_digest(password.encode(), self.password.encode())

    def new_token(self) -> str:
        expires = str(int(time.time()) + MAX_AGE_S)
        return f"{expires}.{self._sign(expires)}"

    def valid(self, token: str | None) -> bool:
        if not self.required:
            return True
        expires, _, signature = (token or "").partition(".")
        return (
            expires.isdigit()
            and hmac.compare_digest(signature, self._sign(expires))
            and int(expires) > time.time()
        )

    def _sign(self, value: str) -> str:
        return hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()
