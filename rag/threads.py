"""
THREADS - riwayat percakapan RAG chat (in-memory per thread).
==============================================================
Terpisah dari `SESSIONS` server (yang punya semantik fase pipeline modul).
Cukup daftar [{role, content}] tanpa fase. Restart server membersihkannya;
dianggap wajar (referensi rag-chatbot juga in-memory).
"""

from typing import Dict, List

from rag import config

# thread_id -> [{role, content}, ...]  (role: "user" | "assistant")
THREADS: Dict[str, List[Dict[str, str]]] = {}


def append_user(thread_id: str, message: str) -> None:
    THREADS.setdefault(thread_id, []).append({"role": "user", "content": message})


def append_assistant(thread_id: str, answer: str) -> None:
    THREADS.setdefault(thread_id, []).append({"role": "assistant", "content": answer})


def clamp(thread_id: str) -> List[Dict[str, str]]:
    """Ambli riwayat dengan klip: max TURNS (percakapan) & CHARS total."""
    history = THREADS.get(thread_id) or []
    max_messages = max(2, config.HISTORY_MAX_TURNS * 2)
    history = history[-max_messages:]
    # klip karakter dari atas: buang pasangan teratas sampai muat budget
    budget = config.HISTORY_MAX_CHARS
    dropped = 0
    while len(history) > 2 and _chars(history) > budget:
        history = history[2:]
        dropped += 1
    return history


def _chars(msgs: List[Dict[str, str]]) -> int:
    return sum(len(m.get("content", "")) for m in msgs)


def build_transcript(thread_id: str, message: str) -> str:
    """Transkrip ringkas utk prompt: riwayat (terklip) + pesan user terakhir."""
    history = clamp(thread_id)
    lines = []
    for m in history:
        who = "User" if m.get("role") == "user" else "Asisten"
        lines.append(f"{who}: {m.get('content', '')}")
    lines.append(f"User: {message}")
    return "\n".join(lines)
