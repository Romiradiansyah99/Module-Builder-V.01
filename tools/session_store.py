"""
SESSION STORE - persistensi sesi server ke file JSON (tahap deployment)
=======================================================================
SESSIONS dulu murni in-memory (InMemorySaver graph): restart server
membuang semua thread/silabus yang sedang berjalan. Modul ini menyimpan
sesi secara atomik ke file JSON di volume (tmp + os.replace) sehingga
restart tidak lagi mematikan sesi tim.

Checkpoint graph sendiri tetap dipisah (SqliteSaver di server.py).

Recovery: sesi yang ditinggal di fase "producing" (crash saat fan-out)
dikembalikan ke "approval" saat load - silabus tetap sah, user tinggal
menekan approve lagi.
"""

import json
import os
import threading
from pathlib import Path
from typing import Dict

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SESSIONS_FILE = _PROJECT_ROOT / "runtime" / "sessions.json"

_LOCK = threading.Lock()


def _file() -> Path:
    """Path file sesi (SESSIONS_FILE env). Path relatif di-resolve terhadap
    root proyek (bukan CWD) agar konsisten dengan konvensi modul lain."""
    val = (os.getenv("SESSIONS_FILE") or "").strip()
    if not val:
        return _SESSIONS_FILE
    p = Path(val)
    return p if p.is_absolute() else _PROJECT_ROOT / p


def load() -> Dict[str, dict]:
    """Muat sesi dari file. Sesi yang tertinggal di fase 'producing'
    dikembalikan ke 'approval' (crash mid-production recovery). File
    hilang/korup -> sesi kosong (server tetap menyala)."""
    path = _file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - korup jangan matikan server
        print(f"[session_store] Gagal memuat {path}: {exc} - mulai dengan sesi kosong")
        return {}
    sessions: Dict[str, dict] = {}
    for tid, sess in (data or {}).items():
        if not isinstance(sess, dict):
            continue
        if sess.get("phase") == "producing":
            sess["phase"] = "approval"
        sessions[tid] = sess
    print(f"[session_store] {len(sessions)} sesi dimuat dari {path}")
    return sessions


def save(sessions: Dict[str, dict]) -> None:
    """Simpan sesi secara atomik (tmp file + os.replace)."""
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _LOCK:
        try:
            tmp.write_text(
                json.dumps(sessions, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            os.replace(str(tmp), str(path))
        except Exception as exc:  # noqa: BLE001 - simpan gagal jangan matikan chat
            print(f"[session_store] Gagal menyimpan sesi: {exc}")
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass