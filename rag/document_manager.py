"""
DOCUMENT MANAGER (RAG CHAT) - upload / list / delete + ingest program.
=====================================================================
Tanpa SQLite: per-doc delete = filter metadata Chroma (`where={"doc_id":...}`),
chunk id stabil = upsert idempoten. Registry baca-balik dari collection.
"""

import logging
import time
from pathlib import Path
from typing import Any, Dict, List

from rag import chunker, config, store

logging.getLogger("pypdf").setLevel(logging.ERROR)

_store: store.VectorStore = None


def get_store() -> store.VectorStore:
    global _store
    if _store is None:
        _store = store.ChromaStore()
    return _store


def _embedder():
    from tools import vector_store as vs

    return vs.get_embedder()


def _embed_texts(texts: List[str]) -> List[List[float]]:
    """Embed dengan batching kecil + retry/backoff (cloud bisa transient)."""
    embedder = _embedder()
    out: List[List[float]] = []
    batch_size = 32
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        delay = 3.0
        for attempt in range(1, 5):
            try:
                out.extend(embedder.embed_documents(batch))
                break
            except Exception as exc:  # noqa: BLE001 - retry backoff
                if attempt == 4:
                    raise
                print(f"[rag] embed batch gagal ({exc.__class__.__name__}) - retry {attempt}/3", flush=True)
                time.sleep(delay)
                delay *= 2
    return out


def ingest(doc_id: str, source_type: str, path: Path, source: str) -> Dict[str, Any]:
    """Ukur lalu simpan dokumen ke rag_chat collection (idempoten per doc_id).

    Menghapus chunk lama doc_id dulu (re-ingest file berubah), lalu upsert
    chunk baru. Return record ringkas utk respons API.
    """
    path = Path(path)
    pages = chunker.load_document(path)
    chunks = chunker.chunk_document(
        doc_id, source_type, source, pages, path=str(path.resolve())
    )
    if not chunks:
        return {"doc_id": doc_id, "source": source, "source_type": source_type,
                "page_count": 0, "chunk_count": 0, "error": "dokumen kosong (tanpa teks)"}

    s = get_store()
    s.delete_by_doc_id(doc_id)  # hapus stale dulu -> upsert tidak menumpuk

    ids = [c["id"] for c in chunks]
    docs = [c["text"] for c in chunks]
    metas = [c["metadata"] for c in chunks]
    embeddings = _embed_texts(docs)
    s.upsert(ids, docs, metas, embeddings)

    page_count = len({c["metadata"]["page"] for c in chunks})
    return {
        "doc_id": doc_id,
        "source": source,
        "source_type": source_type,
        "page_count": page_count,
        "chunk_count": len(chunks),
    }


def delete_document(doc_id: str) -> int:
    """Hapus chunk doc_id + unlink file disk (bila path terekam). Return jumlah chunk."""
    s = get_store()
    data = s.get_by_doc_id(doc_id)
    path = ""
    for meta in data.get("metadatas") or []:
        if meta and meta.get("path"):
            path = meta["path"]
            break
    removed = s.delete_by_doc_id(doc_id)
    if path:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 - file sudah hilang itu wajar
            pass
    return removed


def list_documents() -> List[Dict[str, Any]]:
    """Daftar dokumen di rag_chat, dikelompokkan per doc_id (untuk UI)."""
    grouped = get_store().get_grouped()
    docs = []
    for doc_id, g in grouped.items():
        docs.append({
            "doc_id": doc_id,
            "source": g["source"],
            "source_type": g["source_type"],
            "page_count": g["page_count"],
            "chunk_count": g["chunk_count"],
            "ingested_at": g["ingested_at"],
        })
    docs.sort(key=lambda d: (d.get("ingested_at") or ""), reverse=True)
    return docs


def ingest_program() -> Dict[str, Any] | None:
    """Auto-ingest program docx aktif sebagai source_type="program".

    Best-effort (tak mematikan server): skip bila file tak ada, atau bila
    "program" sudah terindex dengan path yang sama (hindari re-embed berulang).
    """
    from tools.doc_utils import get_active_program

    try:
        prog = get_active_program()
    except Exception:  # noqa: BLE001 - doc_utils belum siap
        return None
    if not prog or not Path(prog).exists():
        return None

    s = get_store()
    existing = s.get_by_doc_id("program")
    if existing.get("metadatas"):
        # sudah terindex: skip kecuali path berubah/program docx diganti
        prog_path = str(Path(prog).resolve())
        for meta in existing.get("metadatas") or []:
            if meta and meta.get("path") == prog_path:
                return None
    try:
        rec = ingest("program", "program", Path(prog), Path(prog).name)
        print(f"[rag] Program docx di-index: {Path(prog).name} ({rec.get('chunk_count', 0)} chunk)")
        return rec
    except Exception as exc:  # noqa: BLE001 - best-effort
        print(f"[rag] Gagal index program: {exc}")
        return None
