"""
KB REFERENSI KEMNAKER - template + contoh modul sebagai data RAG
================================================================
Permintaan user: "take the database into Chroma ... make sure it already
turns out to vector embedding, SKKNI and the template is the data for RAG".

SKKNI sudah lama ter-index di collection `skkni_kemnaker` (tools/vector_store).
Yang BELUM pernah masuk Chroma adalah TEMPLATE modul dan CONTOH MODUL -
padahal keduanya adalah acuan gaya/struktur yang paling sering ditanyakan.
Modul ini menambahkan collection ketiga yang khusus referensi.

Kenapa collection terpisah (bukan menumpang rag_chat / skkni_kemnaker):
- `skkni_kemnaker` hanya menyimpan metadata {source, page} -> tanpa doc_id,
  sehingga hapus-per-dokumen mustahil dan rebuild selalu berarti hapus semua.
- `rag_chat` adalah dokumen USER (mutable). Referensi Kemnaker bersifat
  immutable dan di-ship di dalam image Docker - kelas data yang berbeda.

Dokumen program TIDAK dimasukkan ke sini (RAG_KB_INCLUDE_PROGRAM default 0):
ia sudah ter-index di rag_chat sebagai doc_id "program", dan menaruh chunk
yang identik di dua collection membuat top-k penuh duplikat.

Idempotensi tidak memakai path (path absolut Windows vs Linux berbeda ->
selalu dianggap berubah, re-embed tiap boot, seperti yang terjadi pada
rag_chat). Di sini kunci skip adalah `content_hash` (sha1 byte file), dan
`path` yang disimpan adalah path RELATIF proyek berformat POSIX - portabel
antara Windows dev dan container Linux.
"""

import hashlib
from pathlib import Path
from typing import Any, Dict, List

from rag import chunker, config, store

KB_SCHEMA = "1"

# Ketersediaan TEMPLATE dijamin: korpus contoh modul jauh lebih besar
# (143 dari 151 chunk) dan teksnya prosa asli, sehingga template (8 chunk,
# banyak `{{ tag }}`) selalu kalah skor pada query umum - terukur: template
# tidak pernah masuk top-2. Karena user secara eksplisit meminta template
# jadi data RAG, satu slot template selalu disisipkan bila tidak terambil.
ENSURE_SOURCE_TYPE = "template"

# Embedder yang membangun index ikut ditulis ke metadata SETIAP chunk.
# Kalau EMBEDDING_MODEL diisi di VPS (lihat peringatan docker-compose.yml),
# index 384-d ini akan ditimpa vektor berdimensi lain dan RAG rusak SENYAP.
# Menyimpannya memungkinkan deteksi dini + skip, bukan korupsi.
_EMBEDDER_KEY = "embedder"
_DIM_KEY = "dim"

_store: store.ChromaStore = None


def get_kb_store() -> store.ChromaStore:
    """Store collection KB (singleton proses)."""
    global _store
    if _store is None:
        _store = store.ChromaStore(config.KB_COLLECTION)
    return _store


def _project_root() -> Path:
    from tools import vector_store as vs

    return vs._PROJECT_ROOT


def _rel_path(path: Path) -> str:
    """Path relatif proyek berformat POSIX (portabel Windows <-> Docker)."""
    p = Path(path).resolve()
    try:
        return p.relative_to(_project_root()).as_posix()
    except ValueError:  # di luar root proyek (mis. upload user) - apa adanya
        return p.as_posix()


def doc_fingerprint(path: Path) -> Dict[str, str]:
    """Sidik jari isi file: sha1 byte + ukuran (kunci skip re-ingest)."""
    data = Path(path).read_bytes()
    return {"content_hash": hashlib.sha1(data).hexdigest(), "size": str(len(data))}


def kb_specs() -> List[Dict[str, Any]]:
    """Dokumen referensi tetap + doc_id stabil.

    doc_id memakai slug eksplisit (bukan nama file) karena nama contoh modul
    mengandung spasi dan prefiks "01. " yang rapuh dipakai sebagai id.
    """
    from tools.doc_utils import TEMPLATE_PATH, list_example_modules

    specs: List[Dict[str, Any]] = []
    if TEMPLATE_PATH.exists():
        specs.append({
            "doc_id": "kb:template_kemnaker",
            "source_type": "template",
            "file": TEMPLATE_PATH,
        })
    for i, f in enumerate(list_example_modules(), start=1):
        specs.append({
            "doc_id": f"kb:contoh_modul_{i:02d}",
            "source_type": "contoh_modul",
            "file": f,
        })
    if config.KB_INCLUDE_PROGRAM:
        from tools.doc_utils import get_active_program

        prog = Path(get_active_program() or "")
        if prog.exists():
            specs.append({
                "doc_id": "kb:program",
                "source_type": "program",
                "file": prog,
            })
    return specs


def stored_embedder() -> tuple:
    """(model_name, dim) embedder yang membangun index, atau (None, None)."""
    for meta in get_kb_store().sample_metadata(limit=1):
        return meta.get(_EMBEDDER_KEY), meta.get(_DIM_KEY)
    return None, None


def ingest_kb(force: bool = False) -> Dict[str, Any]:
    """Ingest template + contoh modul ke collection KB (idempoten).

    Skip bila `content_hash` file tidak berubah -> boot berikutnya tidak
    membayar embedding lagi. `force=True` menghapus collection dulu.

    TIDAK PERNAH melempar: pemanggil (lifespan server) harus tetap hidup
    walau KB gagal - kegagalan KB tidak boleh mematikan module-builder.
    """
    result: Dict[str, Any] = {
        "collection": config.KB_COLLECTION,
        "embedded": 0, "skipped": 0, "chunk_count": 0, "docs": [], "error": None,
    }
    try:
        from tools import vector_store as vs

        s = get_kb_store()
        if force:
            s.reset()

        embedder = vs.get_embedder()
        model_name = getattr(embedder, "model_name", "?")

        prev_model, prev_dim = stored_embedder()
        if prev_model and prev_model != model_name:
            result["error"] = (
                f"embedder index ({prev_model}) != embedder aktif ({model_name}) - "
                "ingest KB DILEWATI agar index 384-d tidak rusak. "
                "Kosongkan EMBEDDING_MODEL/EMBEDDING_BASE_URL lalu rebuild."
            )
            print(f"[rag.kb] PERINGATAN: {result['error']}", flush=True)
            result["chunk_count"] = s.count()
            return result

        for spec in kb_specs():
            path = Path(spec["file"])
            fp = doc_fingerprint(path)

            existing = s.get_by_doc_id(spec["doc_id"])
            metas = existing.get("metadatas") or []
            if metas and all(
                (m or {}).get("content_hash") == fp["content_hash"] for m in metas
            ):
                result["skipped"] += 1
                result["docs"].append({
                    "doc_id": spec["doc_id"], "source": path.name,
                    "source_type": spec["source_type"],
                    "chunk_count": len(metas), "status": "skip (tidak berubah)",
                })
                continue

            pages = chunker.load_document(path)
            chunks = chunker.chunk_document(
                spec["doc_id"], spec["source_type"], path.name, pages,
                path=_rel_path(path),
            )
            if not chunks:
                result["docs"].append({
                    "doc_id": spec["doc_id"], "source": path.name,
                    "source_type": spec["source_type"],
                    "chunk_count": 0, "status": "kosong (tanpa teks)",
                })
                continue

            docs = [c["text"] for c in chunks]
            embeddings = _embed_texts(docs)
            dim = str(len(embeddings[0])) if embeddings else ""
            for c in chunks:
                c["metadata"].update({
                    "content_hash": fp["content_hash"],
                    "kb_kind": "reference",
                    "kb_schema": KB_SCHEMA,
                    _EMBEDDER_KEY: model_name,
                    _DIM_KEY: dim,
                })

            s.delete_by_doc_id(spec["doc_id"])  # buang stale sebelum upsert
            s.upsert(
                [c["id"] for c in chunks], docs,
                [c["metadata"] for c in chunks], embeddings,
            )
            result["embedded"] += 1
            result["docs"].append({
                "doc_id": spec["doc_id"], "source": path.name,
                "source_type": spec["source_type"],
                "chunk_count": len(chunks), "dim": dim, "status": "di-index",
            })

        result["chunk_count"] = s.count()
        print(
            f"[rag.kb] KB referensi siap: {result['embedded']} dokumen di-index, "
            f"{result['skipped']} dilewati, total {result['chunk_count']} chunk "
            f"di '{config.KB_COLLECTION}'.",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001 - KB tidak boleh mematikan pemanggil
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(f"[rag.kb] Ingest KB GAGAL (diabaikan): {result['error']}", flush=True)
    return result


def _embed_texts(texts: List[str]) -> List[List[float]]:
    """Reuse batching+retry milik document_manager (jangan duplikasi)."""
    from rag.document_manager import _embed_texts as embed

    return embed(texts)


def _embed_query(text: str) -> List[float]:
    from tools import vector_store as vs

    return vs.get_embedder().embed_query(text)


def kb_retrieve_embedded(query_embedding: List[float], k: int) -> List[Dict[str, Any]]:
    """Top-k KB dari embedding yang SUDAH dihitung (query di-embed sekali).

    Dipakai rag/retriever.py yang sudah punya embedding query - menghindari
    embed ulang (cloud: hemat 1 round-trip per pertanyaan).

    Satu slot disisipkan untuk ENSURE_SOURCE_TYPE (template) bila belum
    terambil, supaya template benar-benar jadi data RAG - bukan hanya
    "ada di Chroma tapi tak pernah terpakai".
    """
    if k <= 0:
        return []
    s = get_kb_store()

    def _clean(docs):
        return [
            d for d in docs
            if d.get("content", "").strip() and d.get("source_type") != "kb_meta"
        ]

    docs = _clean(s.query(query_embedding, k=k))
    if not docs or not ENSURE_SOURCE_TYPE:
        return docs

    if not any(d.get("source_type") == ENSURE_SOURCE_TYPE for d in docs):
        extra = _clean(s.query(query_embedding, k=1,
                               where={"source_type": ENSURE_SOURCE_TYPE}))
        if extra:
            # ambil (k-1) teratas + slot template -> template pasti ikut,
            # peringkat sisanya tidak berubah relatif satu sama lain.
            docs = docs[: max(0, k - 1)] + extra
    return docs


def kb_retrieve(query: str, k: int = None) -> List[Dict[str, Any]]:
    """Top-k KB dari teks query (kepraktisan CLI / verifier)."""
    k = config.RAG_TOP_K_KB if k is None else k
    if k <= 0 or not (query or "").strip():
        return []
    return kb_retrieve_embedded(_embed_query(query), k)


def kb_status() -> Dict[str, Any]:
    """Ringkasan KB dari metadata Chroma (tanpa menyentuh jaringan)."""
    try:
        s = get_kb_store()
        grouped = s.get_grouped()
        docs = [
            {
                "doc_id": g["doc_id"], "source": g["source"],
                "source_type": g["source_type"], "chunk_count": g["chunk_count"],
            }
            for g in grouped.values()
            if g["source_type"] != "kb_meta"
        ]
        docs.sort(key=lambda d: d["doc_id"])
        model_name, dim = stored_embedder()
        return {
            "ready": bool(docs), "collection": config.KB_COLLECTION,
            "chunk_count": sum(d["chunk_count"] for d in docs),
            "doc_count": len(docs), "docs": docs,
            "embedder": model_name, "dim": dim, "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ready": False, "collection": config.KB_COLLECTION,
                "chunk_count": 0, "doc_count": 0, "docs": [],
                "error": f"{type(exc).__name__}: {exc}"}


# ----------------------------------------------------------------------
# CLI: python -m rag.kb [--force] ["query"]
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    force = "--force" in args
    rest = [a for a in args if a != "--force"]

    if rest:
        hits = kb_retrieve(" ".join(rest), k=5)
        print(f"\nTop-{len(hits)} untuk: {' '.join(rest)}")
        for i, h in enumerate(hits, 1):
            print(f"  {i}. [{h['score']:.3f}] {h['source']} "
                  f"({h['source_type']}) - {h['content'][:110]}...")
    else:
        rec = ingest_kb(force=force)
        print(f"\nStatus: {kb_status()}")
        if rec.get("error"):
            raise SystemExit(1)
