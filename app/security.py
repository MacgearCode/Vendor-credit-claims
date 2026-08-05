"""Password hashing (PBKDF2, stdlib — no native deps), signed-cookie and reset-token helpers."""
import base64
import hashlib
import hmac
import os
import secrets

_ITER = 200_000


def hash_password(pw: str, iterations: int = _ITER) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def sign(value: str, secret: str) -> str:
    sig = hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{sig}"


def unsign(token: str, secret: str) -> str | None:
    if not token or "." not in token:
        return None
    value, _, sig = token.rpartition(".")
    expected = hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()
    return value if hmac.compare_digest(sig, expected) else None


def make_reset_token() -> str:
    """A high-entropy, URL-safe token sent (raw) in the reset link email."""
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    """What we persist — we never store the raw token, only its SHA-256 digest."""
    return hashlib.sha256(raw.encode()).hexdigest()
