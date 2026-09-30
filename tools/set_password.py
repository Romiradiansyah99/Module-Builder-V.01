"""
SET PASSWORD - pasang sandi login ke .env tanpa menampilkannya
==============================================================
Dipakai SEBELUM aplikasi diekspos ke internet (Cloudflare Tunnel, VPS, LAN):
tanpa `APP_PASSWORD_SHA256` + `SESSION_SECRET`, `require_auth()` di server.py
mengembalikan lebih awal sehingga SELURUH rute terbuka tanpa login - siapa pun
yang punya URL bisa memicu pipeline LLM berbayar dan membaca dokumen RAG.

Sandi dibaca lewat getpass: tidak tampil di layar, tidak masuk history shell,
dan tidak pernah masuk transkrip chat. Yang ditulis ke .env hanya HASH-nya
(SHA-256), sama seperti yang dibandingkan server saat login.

Pakai:
    python tools/set_password.py            # tanya sandi (tanpa echo)
    python tools/set_password.py --cek      # hanya laporkan status auth, tanpa mengubah

Aman dijalankan berulang: nilai lama di .env ditimpa di tempat, bukan
ditambahkan (tidak ada kunci duplikat).
"""

import hashlib
import getpass
import re
import secrets
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"

# Kunci yang dikelola skrip ini; sisanya tidak pernah disentuh.
_KEYS = ("APP_PASSWORD_SHA256", "SESSION_SECRET", "COOKIE_SECURE")


def _read_env() -> str:
    if not _ENV_PATH.exists():
        raise SystemExit(f"[set_password] .env tidak ada di {_ENV_PATH}")
    # newline="" -> jangan normalisasi akhir baris file yang sudah ada.
    return _ENV_PATH.read_text(encoding="utf-8", errors="replace")


def _get(text: str, key: str) -> str:
    m = re.search(rf"^{key}=(.*)$", text, re.M)
    return m.group(1).strip() if m else ""


def _put(text: str, key: str, value: str) -> str:
    """Timpa `key` di tempat; tambahkan di akhir bila belum ada.

    Ditimpa di tempat (bukan append) supaya kunci tidak pernah duplikat -
    duplikat membuat nilai yang dibaca dotenv tidak terduga.
    """
    line = f"{key}={value}"
    if re.search(rf"^{key}=", text, re.M):
        return re.sub(rf"^{key}=.*$", line, text, count=1, flags=re.M)
    sep = "" if text.endswith("\n") or not text else "\n"
    return f"{text}{sep}{line}\n"


def status() -> bool:
    """True bila auth sudah aktif. Hanya membaca, tidak mengubah."""
    text = _read_env()
    has_hash = bool(_get(text, "APP_PASSWORD_SHA256"))
    has_secret = bool(_get(text, "SESSION_SECRET"))
    print(f"[set_password] APP_PASSWORD_SHA256 : {'ADA' if has_hash else 'KOSONG'}")
    print(f"[set_password] SESSION_SECRET       : {'ADA' if has_secret else 'KOSONG'}")
    if has_hash and has_secret:
        print("[set_password] Auth AKTIF - aplikasi meminta login.")
        return True
    print("[set_password] Auth BELUM aktif - JANGAN diekspos ke internet.")
    return False


def set_password(password: str) -> None:
    """Tulis hash sandi + SESSION_SECRET ke .env. Tidak mencetak keduanya."""
    if not password:
        raise SystemExit("[set_password] sandi kosong - dibatalkan.")
    if len(password) < 8:
        raise SystemExit("[set_password] sandi minimal 8 karakter - dibatalkan.")

    text = _read_env()
    # Penanda bagian hanya bila blok auth belum ada sama sekali.
    if not any(_get(text, k) for k in _KEYS):
        sep = "" if text.endswith("\n") else "\n"
        text = f"{text}{sep}\n# --- Auth (dibuat tools/set_password.py) ---\n"

    text = _put(text, "APP_PASSWORD_SHA256", hashlib.sha256(
        password.encode("utf-8")).hexdigest())

    # SESSION_SECRET = penanda tangan cookie sesi. Pertahankan yang sudah ada:
    # menggantinya memaksa semua sesi login lama gugur tanpa alasan.
    if not _get(text, "SESSION_SECRET"):
        text = _put(text, "SESSION_SECRET", secrets.token_hex(32))
        print("[set_password] SESSION_SECRET dibuat baru (64 hex).")

    # trycloudflare/Caddy sudah HTTPS ke browser; cookie Secure boleh 0.
    if not _get(text, "COOKIE_SECURE"):
        text = _put(text, "COOKIE_SECURE", "0")

    _ENV_PATH.write_text(text, encoding="utf-8")
    print("[set_password] APP_PASSWORD_SHA256 ditulis ke .env (hash, bukan sandi).")
    print("[set_password] Restart aplikasi agar berlaku: python server.py")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--cek" in sys.argv:
        return 0 if status() else 1
    if not status():
        print()
    p1 = getpass.getpass("Sandi baru (tidak tampil): ")
    p2 = getpass.getpass("Ulangi sandi             : ")
    if p1 != p2:
        raise SystemExit("[set_password] sandi tidak sama - dibatalkan, tidak ada yang diubah.")
    set_password(p1)
    print()
    status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
