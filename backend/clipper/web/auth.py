"""Password login with a signed, long-lived cookie. The app is reached privately (Tailscale), so one shared password is enough."""

import base64
import hashlib
import hmac
import json
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

    def sign_data(self, data: dict) -> str:
        """`data` as a token that can't be altered, e.g. to carry through Google's sign-in and back."""
        payload = base64.urlsafe_b64encode(json.dumps({**data, "at": int(time.time())}).encode()).decode()
        return f"{payload}.{self._sign(payload)}"

    def read_data(self, token: str, max_age_s: int) -> dict | None:
        """The data in a `sign_data` token; None if it was altered or is older than `max_age_s`."""
        payload, _, signature = token.partition(".")
        if not payload or not hmac.compare_digest(signature, self._sign(payload)):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data if time.time() - data["at"] <= max_age_s else None

    def _sign(self, value: str) -> str:
        return hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()
