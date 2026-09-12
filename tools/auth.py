"""
AUTH - kata sandi tunggal utk deployment tim (tahap deployment)
===============================================================
Satu sandi bersama untuk seluruh tim: cookie sesi ditandatangani
itsdangerous (HttpOnly, SameSite=Lax). Semua rute dilindungi lewat
satu dependency FastAPI; pengecualian hanya halaman login, endpoint
login, dan /healthz (dipakai Docker healthcheck).

Env:
  APP_PASSWORD_SHA256  hex SHA-256 kata sandi (produksi - disarankan)
  APP_PASSWORD         kata sandi polos (dev - di-hash saat boot)
  SESSION_SECRET       secret penanda tangan cookie (openssl rand -hex 32)
  COOKIE_SECURE        "1" bila di belakang HTTPS -> cookie Secure
"""

import hashlib
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

COOKIE_NAME = "kb_session"
_COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 hari


def _password_sha256() -> Optional[str]:
    """Hash kata sandi dari env. SHA-256 diutamakan; APP_PASSWORD polos
    di-hash saat boot (hanya untuk dev)."""
    sha = (os.getenv("APP_PASSWORD_SHA256") or "").strip().lower()
    if sha:
        return sha
    plain = (os.getenv("APP_PASSWORD") or "").strip()
    if plain:
        return hashlib.sha256(plain.encode("utf-8")).hexdigest()
    return None


def _serializer() -> Optional[URLSafeTimedSerializer]:
    secret = (os.getenv("SESSION_SECRET") or "").strip()
    if not secret:
        return None
    return URLSafeTimedSerializer(secret, salt="kemnaker-module-builder")


def auth_configured() -> bool:
    """True bila sandi + secret tersedia. Tanpa keduanya server tetap jalan
    tanpa auth (mode dev lokal) - deployment wajib mengisi keduanya."""
    return _password_sha256() is not None and _serializer() is not None


def check_password(candidate: str) -> bool:
    """Bandingkan kata sandi kandidat dgn hash env (konstanta-waktu)."""
    expected = _password_sha256()
    if not expected or not candidate:
        return False
    candidate_sha = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    # hmac.compare_digest: bandingkan hash tanpa kebocoran timing.
    import hmac

    return hmac.compare_digest(candidate_sha, expected)


def make_session_token() -> str:
    """Token cookie ditandatangani (berisi tanda login, bukan data sensitif)."""
    return _serializer().dumps({"login": True})


def verify_session_token(token: Optional[str]) -> bool:
    if not token:
        return False
    try:
        data = _serializer().loads(token, max_age=_COOKIE_MAX_AGE)
        return bool(data.get("login"))
    except (BadSignature, SignatureExpired, Exception):  # noqa: BLE001
        return False


def cookie_kwargs() -> dict:
    """Atribut cookie sesi. Secure hanya bila COOKIE_SECURE=1 (HTTPS/Caddy)."""
    return {
        "key": COOKIE_NAME,
        "httponly": True,
        "samesite": "lax",
        "secure": os.getenv("COOKIE_SECURE", "0").strip() == "1",
        "max_age": _COOKIE_MAX_AGE,
        "path": "/",
    }