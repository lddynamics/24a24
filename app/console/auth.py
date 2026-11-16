"""Authentification de la console : mot de passe unique (démo) + cookie de session signé.

Le cookie ne contient que « expiration.nonce.signature ». La clé de signature dérive de la clé de chiffrement
de l'application ET du mot de passe : changer le mot de passe invalide toutes les sessions.
"""
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from app.config import get_settings

COOKIE = "a24_console"


def enabled() -> bool:
    s = get_settings()
    return bool(s.console_password and s.console_tenant_id)


def _key() -> bytes:
    s = get_settings()
    return hashlib.sha256(f"console-session|{s.encryption_key}|{s.console_password}".encode()).digest()


def _sign(payload: str) -> str:
    return hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(now: float | None = None) -> str:
    exp = int((now if now is not None else time.time()) + get_settings().console_session_hours * 3600)
    payload = f"{exp}.{secrets.token_hex(8)}"
    return f"{payload}.{_sign(payload)}"


def verify_token(token: str | None, now: float | None = None) -> bool:
    if not token or not enabled():
        return False
    try:
        exp, nonce, sig = token.split(".")
        return hmac.compare_digest(_sign(f"{exp}.{nonce}"), sig) and int(exp) > (now if now is not None else time.time())
    except ValueError:
        return False


def check_password(candidate: str) -> bool:
    expected = get_settings().console_password
    return bool(expected) and hmac.compare_digest(candidate.encode(), expected.encode())


class LoginLimiter:
    """Freine les essais de mot de passe : `limit` tentatives par fenêtre de `window` secondes et par clé."""

    def __init__(self, limit: int = 5, window: int = 300):
        self.limit, self.window = limit, window
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def blocked(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        hits = self._hits[key]
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        return len(hits) >= self.limit

    def record_failure(self, key: str, now: float | None = None) -> None:
        self._hits[key].append(time.time() if now is None else now)

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


limiter = LoginLimiter()
