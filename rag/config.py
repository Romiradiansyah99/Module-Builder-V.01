"""
KONFIGURASI RAG CHAT - satu tempat baca seluruh .env RAG.
Relatif path di-resolve terhadap root proyek, konsisten dgn server.py.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


def _resolve_env_path(name: str, default: Path) -> Path:
    val = (os.getenv(name) or "").strip()
    if not val:
        return default
    p = Path(val)
    return p if p.is_absolute() else _PROJECT_ROOT / p


# Collection rag_chat (terpisah dari skkni_kemnaker yang dibiarkan utuh).
CHAT_COLLECTION = os.getenv("RAG_CHAT_COLLECTION", "rag_chat").strip()
RAG_TOP_K_CHAT = int(os.getenv("RAG_TOP_K_CHAT", "6"))
CONTEXT_STRATEGY = os.getenv("RAG_CONTEXT_STRATEGY", "create-and-refine").strip()
CONTEXT_MAX_CHARS = int(os.getenv("RAG_CONTEXT_MAX_CHARS", "8000"))
HISTORY_MAX_TURNS = int(os.getenv("RAG_HISTORY_MAX_TURNS", "6"))
HISTORY_MAX_CHARS = int(os.getenv("RAG_HISTORY_MAX_CHARS", "4000"))
ANSWER_TEMPERATURE = float(os.getenv("RAG_ANSWER_TEMPERATURE", "0.3"))
ANSWER_MAX_TOKENS = int(os.getenv("RAG_ANSWER_MAX_TOKENS", "16000"))
REFINE_MAX_TOKENS = int(os.getenv("RAG_REFINE_MAX_TOKENS", "6000"))

# ----------------------------------------------------------------------
# KB REFERENSI Kemnaker (template + contoh modul) - collection TERPISAH.
# Immutable: di-ingest sekali, di-ship di dalam image Docker bersama
# database/chroma_db. Dokumen program TIDAK dimasukkan ke sini karena
# sudah ter-index di rag_chat (doc_id "program") - duplikat akan mengisi
# top-k dengan chunk yang nyaris identik.
# ----------------------------------------------------------------------
KB_COLLECTION = os.getenv("RAG_KB_COLLECTION", "template_kemnaker").strip()
RAG_TOP_K_KB = int(os.getenv("RAG_TOP_K_KB", "2"))
KB_INGEST_ON_BOOT = os.getenv("RAG_KB_INGEST_ON_BOOT", "1").strip() == "1"
KB_INCLUDE_PROGRAM = os.getenv("RAG_KB_INCLUDE_PROGRAM", "0").strip() == "1"

UPLOADS_DIR = _resolve_env_path(
    "UPLOADS_DIR", _PROJECT_ROOT / "database" / "uploads"
) / "rag_chat"

# Cache gambar hasil generate (Replicate) - di bawah runtime/ agar ikut
# volume Docker dan tidak dibayar dua kali untuk prompt yang sama.
IMAGE_CACHE_DIR = _resolve_env_path(
    "IMAGE_CACHE_DIR", _PROJECT_ROOT / "runtime" / "image_cache"
)

PROGRAM_DOC_PATH = _resolve_env_path(
    "RAG_PROGRAM_DOC_PATH",
    _PROJECT_ROOT / "database" / "program" / "Program_Pelatihan_PLTSa_Revisi_Kemenaker.docx",
)

# Path Chroma memakai konfigurasi vector_store yang sama (CHROMA_DB_DIR).
def chroma_db_dir() -> Path:
    from tools import vector_store as vs

    return vs.CHROMA_DB_DIR
