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
import secrets
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

COOKIE_NAME = "kb_session"
_COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 hari

# ----------------------------------------------------------------------
# IDENTITAS PRIVAT PER-BROWSER (kb_owner)
# ----------------------------------------------------------------------
# Cookie KEDUA yang menyimpan id acak tak-terka (riwayat percakapan dipakai
# sebagai pemilik thread). Sengaja DIPISAH dari kb_session: logout harus
# menghapus sesi login, tapi identitas HARUS bertahan supaya riwayat kembali
# saat login lagi di browser yang sama. Ditandatangani SESSION_SECRET yang
# sama tapi dengan SALT BERBEDA (pemisahan ranah: token sesi tak bisa
# diputar-ulang jadi token pemilik) - jadi tidak perlu merotasi secret.
OWNER_COOKIE_NAME = "kb_owner"
_OWNER_COOKIE_MAX_AGE = 400 * 24 * 3600  # ~400 hari (plafon cookie Chrome)
DEV_OWNER = "local-dev"  # identitas tunggal saat auth dimatikan (dev lokal)


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


def _owner_serializer() -> Optional[URLSafeTimedSerializer]:
    """Penanda tangan cookie identitas. Salt BEDA dari cookie sesi."""
    secret = (os.getenv("SESSION_SECRET") or "").strip()
    if not secret:
        return None
    return URLSafeTimedSerializer(secret, salt="kemnaker-owner")


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


# ----------------------------------------------------------------------
# Cookie identitas (kb_owner) - privat per-browser, tahan logout
# ----------------------------------------------------------------------


def new_owner_id() -> str:
    """Id identitas acak 256-bit - tak terka, tak diturunkan dari apa pun."""
    return secrets.token_urlsafe(32)


def make_owner_token(owner_id: str) -> str:
    """Token cookie identitas (ditandatangani, bukan data sensitif)."""
    return _owner_serializer().dumps({"owner": owner_id})


def verify_owner_token(token: Optional[str]) -> Optional[str]:
    """Kembalikan id pemilik dari token, atau None. Tak pernah melempar."""
    if not token:
        return None
    try:
        data = _owner_serializer().loads(token, max_age=_OWNER_COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired, Exception):  # noqa: BLE001
        return None
    oid = data.get("owner")
    return oid if isinstance(oid, str) and oid else None


def owner_cookie_kwargs() -> dict:
    """Atribut cookie identitas. Umurnya jauh lebih panjang dari sesi."""
    return {
        "key": OWNER_COOKIE_NAME,
        "httponly": True,
        "samesite": "lax",
        "secure": os.getenv("COOKIE_SECURE", "0").strip() == "1",
        "max_age": _OWNER_COOKIE_MAX_AGE,
        "path": "/",
    }


def current_owner(request) -> Optional[str]:
    """Id pemilik dari cookie permintaan ini (None bila belum ada)."""
    return verify_owner_token(request.cookies.get(OWNER_COOKIE_NAME))


def resolve_owner_for_login(request) -> str:
    """Pakai ulang identitas browser bila ada; kalau tidak, buat yang baru.

    Inilah yang membuat riwayat 'kembali' saat login lagi di browser sama."""
    return current_owner(request) or new_owner_id()


def clear_session_cookie_kwargs() -> dict:
    """Atribut untuk MENGHAPUS cookie sesi saat logout.

    kb_owner SENGAJA tidak disentuh - identitas harus bertahan agar riwayat
    kembali saat login lagi."""
    return {
        "key": COOKIE_NAME,
        "path": "/",
        "httponly": True,
        "samesite": "lax",
        "secure": os.getenv("COOKIE_SECURE", "0").strip() == "1",
    }