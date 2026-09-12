"""
AI MEMORY - ilmu "bersikap" lintas sesi (ronde 5)
==================================================
Setelah tiap pertukaran percakapan, server merangkum obrolan menjadi
ATURAN SIKAP (maks 8 poin) di background: gaya bahasa yang user harapkan,
koreksi yang user berikan, inisiatif yang dihargai. Aturan ini disimpan di
database/ai_memory.json dan disuntikkan ke prompt Agent 1 (dig & build)
sehingga AI "tumbuh" memahami bagaimana sebaiknya bersikap untuk user ini.

Gagal aman: LLM mati/jaringan putus -> memori lama tetap dipakai, sistem
tetap jalan tanpa memori.
"""

import json
import os
import threading
from pathlib import Path
from typing import List, Optional

_MEMORY_PATH = Path(
    os.getenv("AI_MEMORY_PATH", str(Path(__file__).resolve().parent.parent / "database" / "ai_memory.json"))
)
_LOCK = threading.Lock()
_MAX_LESSONS = 8
_HISTORY_CAP = 16  # pesan terakhir yang dikirim ke kurator

_MEMORY_PROMPT = """Anda kurator memori SIKAP untuk asisten AI pembuat modul pelatihan Kemnaker.

Diberikan aturan sikap LAMA dan percakapan TERBARU dengan user, perbarui daftar
aturan: bagaimana asisten sebaiknya BERSIKAP untuk user ini - gaya bahasa,
tingkat kepaduan, inisiatif yang user harapkan (mis. menawarkan mengisi data
kosong), koreksi yang user sampaikan, hal yang menyebalkan bagi user.

ATURAN SIKAP LAMA:
{old}

PERCAKAPAN TERBARU:
{convo}

ATURAN:
- Maksimal {_max} poin, tiap poin satu kalimat ringkas Bahasa Indonesia.
- Pertahankan aturan lama yang masih relevan; perbarui/perganti yang terbantahkan
  oleh percakapan terbaru; tambahkan yang baru.
- Hanya aturan SIKAP/PERILAKU - bukan catatan isi program pelatihan.

Jawab HANYA satu objek JSON, tanpa teks lain:
{{"lessons": ["...", "..."]}}"""


def load() -> dict:
    """Muat memori; file hilang/rusak -> kosong (sistem tetap jalan)."""
    try:
        data = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("lessons"), list):
            return {"lessons": [str(x) for x in data["lessons"][:_MAX_LESSONS]]}
    except Exception:  # noqa: BLE001 - memori rusak dianggap kosong
        pass
    return {"lessons": []}


def _save(lessons: List[str]) -> None:
    _MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MEMORY_PATH.write_text(
        json.dumps({"lessons": lessons[:_MAX_LESSONS]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_style_block() -> str:
    """Blok teks untuk prompt Agent 1; '' bila memori masih kosong."""
    lessons = load()["lessons"]
    if not lessons:
        return ""
    lines = ["=== GAYA & PENGALAMAN (dari sesi-sesi sebelumnya - IKUTI) ==="]
    lines += [f"- {x}" for x in lessons]
    return "\n".join(lines)


def update_from_conversation(history: List[dict]) -> bool:
    """Rangkum percakapan -> perbarui aturan sikap (panggil di BACKGROUND).

    Return True bila memori berhasil diperbarui. Tak pernah melempar
    exception - kegagalan LLM/network hanya dicatat ke log."""
    msgs = [m for m in (history or []) if isinstance(m, dict) and m.get("content")]
    if len(msgs) < 2:  # butuh minimal 1 pasang tanya-jawab
        return False
    convo = "\n".join(
        f"{'USER' if m.get('role') == 'user' else 'ASSISTANT'}: {str(m['content'])[:600]}"
        for m in msgs[-_HISTORY_CAP:]
    )
    old = load()["lessons"]
    old_text = ("\n".join(f"- {x}" for x in old) if old else "(belum ada)")
    prompt = _MEMORY_PROMPT.format(old=old_text, convo=convo, _max=_MAX_LESSONS)
    try:
        from tools.llm_config import get_llm_for_effort, invoke_with_retry

        llm = get_llm_for_effort("low")  # model cepat - rangkuman murah
        resp = invoke_with_retry(llm, prompt, attempts=2, label="memory")
        text = resp.content
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return False
        data = json.loads(text[start : end + 1])
        lessons = [str(x).strip() for x in data.get("lessons", []) if str(x).strip()]
        if not lessons:
            return False
        with _LOCK:
            _save(lessons)
        print(f"[ai_memory] aturan sikap diperbarui ({len(lessons)} poin)")
        return True
    except Exception as exc:  # noqa: BLE001 - memori tak boleh mengganggu chat
        print(f"[ai_memory] pembaruan gagal (diabaikan): {type(exc).__name__}: {exc}")
        return False


def start_background_update(history: List[dict]) -> Optional[threading.Thread]:
    """Apikan pembaruan memori di thread daemon - tak memblokir respons."""
    th = threading.Thread(target=update_from_conversation, args=(list(history or []),), daemon=True)
    th.start()
    return th