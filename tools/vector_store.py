"""
VECTOR STORE - RAG ChromaDB untuk dokumen SKKNI
================================================
Tanggung jawab modul ini:
1. Menjalankan (ingest) semua PDF SKKNI di folder `database/skkni_docs`
   ke dalam ChromaDB persistent (`database/chroma_db`).
2. Menyediakan retriever untuk Agent 1 (Syllabus Creator) agar silabus
   yang dihasilkan selalu berpijak pada elemen kompetensi SKKNI asli.

STRATEGI EMBEDDING (penting):
- Prioritas: embedding model Ollama-compatible sesuai konfigurasi .env
  (EMBEDDING_BASE_URL + EMBEDDING_MODEL), sehingga provider cloud bisa
  berganti sewaktu-waktu tanpa ubah kode.
- Fallback: jika endpoint/mode embedding tidak tersedia, otomatis memakai
  embedding bawaan ChromaDB (all-MiniLM ONNX, jalan 100% lokal).
- PENTING: ingest dan query WAJIB memakai embedder yang sama. Pilihan
  embedder di-cache di level modul setelah probe pertama berhasil.
"""

import os
from pathlib import Path
from typing import Any, Callable, Dict, List

import logging
from dotenv import load_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

# Bisukan warning fontTools/pypdf yang membanjiri console
logging.getLogger("pypdf").setLevel(logging.ERROR)

# Muat .env dari root proyek (parent dari folder tools/)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

SKKNI_DOCS_DIR = _PROJECT_ROOT / os.getenv("SKKNI_DOCS_DIR", "database/skkni_docs")
CHROMA_DB_DIR = _PROJECT_ROOT / os.getenv("CHROMA_DB_DIR", "database/chroma_db")
COLLECTION_NAME = os.getenv("CHROMA_COLLECTION", "skkni_kemnaker")
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "5"))
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1200"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "200"))


def _ingest_batch_size() -> int:
    """Batch size adaptif: endpoint cloud (remote) pakai batch kecil agar
    tidak kena timeout/rate-limit; Ollama lokal pakai batch besar."""
    configured = os.getenv("INGEST_BATCH_SIZE", "")
    if configured.isdigit() and int(configured) > 0:
        return int(configured)
    base_url = os.getenv("EMBEDDING_BASE_URL", "")
    remote = bool(base_url) and not any(h in base_url for h in ("localhost", "127.0.0.1"))
    return 32 if remote else 256


# ----------------------------------------------------------------------
# Embedding: adapter dengan interface seragam
# ----------------------------------------------------------------------
class _OllamaEmbedder:
    """Embedding via endpoint Ollama-compatible (sesuai .env)."""

    def __init__(self, model: str, base_url: str, api_key: str) -> None:
        from langchain_ollama import OllamaEmbeddings

        self.model_name = model
        self._ef = OllamaEmbeddings(
            model=model,
            base_url=base_url,
            client_kwargs={"headers": {"Authorization": f"Bearer {api_key}"}},
        )

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return self._ef.embed_documents(texts)

    def embed_query(self, text: str) -> List[float]:
        return self._ef.embed_query(text)


class _ChromaDefaultEmbedder:
    """Fallback: embedding bawaan ChromaDB (all-MiniLM ONNX, lokal)."""

    def __init__(self) -> None:
        from chromadb.utils import embedding_functions

        self.model_name = "chroma-default-all-MiniLM"
        self._ef = embedding_functions.DefaultEmbeddingFunction()

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        # cast float() eksplisit: chromadb>=1.5 menolak elemen np.float32 di dalam list
        return [[float(x) for x in e] for e in self._ef(texts)]

    def embed_query(self, text: str) -> List[float]:
        return [float(x) for x in self._ef([text])[0]]


_embedder_cache = None


def get_embedder():
    """Embedder seragam untuk ingest & query.

    EMBEDDING_MODEL boleh berisi beberapa kandidat dipisah koma. Tiap
    kandidat bisa polos ("nomic-embed-text" -> pakai EMBEDDING_BASE_URL)
    atau dengan endpoint eksplisit ("nomic-embed-text@http://localhost:11434").
    Masing-masing di-probe dengan satu embed_query murah; kandidat pertama
    yang berhasil dipakai (di-cache untuk seluruh proses). Kalau semua
    kandidat gagal, jatuh ke embedding lokal ChromaDB.
    """
    global _embedder_cache
    if _embedder_cache is not None:
        return _embedder_cache

    default_base_url = os.getenv("EMBEDDING_BASE_URL", "")
    models_csv = os.getenv("EMBEDDING_MODEL", "")
    api_key = os.getenv("EMBEDDING_API_KEY", "")

    if models_csv:
        for raw in models_csv.split(","):
            raw = raw.strip()
            if not raw:
                continue
            # Sintaks "model@endpoint" -> endpoint per-kandidat
            if "@" in raw:
                model, base_url = raw.split("@", 1)
            else:
                model, base_url = raw, default_base_url
            try:
                candidate = _OllamaEmbedder(model.strip(), base_url.strip(), api_key)
                candidate.embed_query("tes koneksi embedding")  # probe murah
                print(f"[vector_store] Embedder aktif: {model} @ {base_url}", flush=True)
                _embedder_cache = candidate
                return _embedder_cache
            except Exception as exc:  # noqa: BLE001 - lanjut kandidat berikutnya
                print(
                    f"[vector_store] Kandidat embedding '{model} @ {base_url}' gagal "
                    f"({exc.__class__.__name__}: {str(exc)[:150]})",
                    flush=True,
                )
        print(
            "[vector_store] Semua kandidat embedding gagal - jatuh ke embedding lokal ChromaDB.",
            flush=True,
        )
    _embedder_cache = _ChromaDefaultEmbedder()
    print(f"[vector_store] Embedder aktif: {_embedder_cache.model_name} (lokal)", flush=True)
    return _embedder_cache


# ----------------------------------------------------------------------
# ChromaDB persistent client
# ----------------------------------------------------------------------
def get_collection():
    """Ambil (atau buat) collection ChromaDB dengan similarity cosine."""
    import chromadb  # import di sini agar modul tetap bisa di-import tanpa chromadb terpasang

    client = chromadb.PersistentClient(path=str(CHROMA_DB_DIR))
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def _reset_collection():
    import chromadb

    chromadb.PersistentClient(path=str(CHROMA_DB_DIR)).delete_collection(COLLECTION_NAME)
    return get_collection()


# ----------------------------------------------------------------------
# PDF loading & chunking
# ----------------------------------------------------------------------
def _load_pdf_pages(pdf_path: Path) -> List[Dict[str, Any]]:
    """Ekstrak teks per-halaman dari satu file PDF."""
    reader = PdfReader(str(pdf_path))
    pages = []
    for i, page in enumerate(reader.pages):
        text = (page.extract_text() or "").strip()
        if text:  # lewati halaman kosong (scan gambar tanpa OCR)
            pages.append({"page": i + 1, "text": text, "source": pdf_path.name})
    return pages


def ingest_skkni_docs(force: bool = False) -> Dict[str, int]:
    """Jalankan semua PDF SKKNI ke dalam ChromaDB.

    - Idempotent: chunk ID stabil (`{file}::p{page}::c{index}`), sehingga
      ingest ulang menimpa (upsert), bukan menduplikasi.
    - `force=True` menghapus collection lama dulu (rebuild bersih).
    - Jika collection sudah terisi dan `force=False`, skip (no-op).

    Return: {"files": n, "pages": n, "chunks": n}
    """
    collection = get_collection()
    if force:
        collection = _reset_collection()

    if collection.count() > 0 and not force:
        print(
            f"[vector_store] Sudah ada {collection.count()} chunk — "
            f"skip ingest (pakai force=True untuk rebuild)."
        )
        return {"files": 0, "pages": 0, "chunks": collection.count()}

    pdf_files = sorted(SKKNI_DOCS_DIR.glob("*.pdf"))
    if not pdf_files:
        raise FileNotFoundError(
            f"Tidak ada PDF di {SKKNI_DOCS_DIR}. Letakkan dokumen SKKNI di folder tersebut."
        )

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " ", ""],
    )

    all_chunks: List[Dict[str, str]] = []
    total_pages = 0
    for pdf_path in pdf_files:
        pages = _load_pdf_pages(pdf_path)
        total_pages += len(pages)
        print(f"[vector_store] {pdf_path.name}: {len(pages)} halaman teks")
        for page in pages:
            for idx, chunk in enumerate(splitter.split_text(page["text"])):
                all_chunks.append(
                    {
                        "id": f"{page['source']}::p{page['page']}::c{idx}",
                        "text": chunk,
                        "source": page["source"],
                        "page": str(page["page"]),
                    }
                )

    embedder = get_embedder()
    n = len(all_chunks)
    batch_size = _ingest_batch_size()
    print(f"[vector_store] Total {n} chunk - mulai embedding (batch={batch_size}, model={embedder.model_name})...", flush=True)

    import time

    for start in range(0, n, batch_size):
        batch = all_chunks[start : start + batch_size]

        # Embed dengan retry & exponential backoff (cloud bisa transient error).
        delay = 3.0
        for attempt in range(1, 5):
            try:
                embeddings = embedder.embed_documents([c["text"] for c in batch])
                break
            except Exception as exc:  # noqa: BLE001 - retry dengan backoff
                if attempt == 4:
                    raise
                print(
                    f"[vector_store] Batch {start//batch_size + 1} gagal "
                    f"({exc.__class__.__name__}) - retry {attempt}/3 dalam {delay:.0f}s",
                    flush=True,
                )
                time.sleep(delay)
                delay *= 2

        collection.upsert(
            ids=[c["id"] for c in batch],
            documents=[c["text"] for c in batch],
            metadatas=[{"source": c["source"], "page": c["page"]} for c in batch],
            embeddings=embeddings,
        )
        done = min(start + batch_size, n)
        print(f"[vector_store] Progress ingest: {done}/{n} chunk", flush=True)

    print()
    stats = {"files": len(pdf_files), "pages": total_pages, "chunks": n}
    print(f"[vector_store] Ingest selesai: {stats}")
    return stats


# ----------------------------------------------------------------------
# Retriever (dipakai Agent 1)
# ----------------------------------------------------------------------
def get_retriever(top_k: int = None) -> Callable[[str], List[Dict[str, Any]]]:
    """Kembalikan callable retriever.

    Pakai:
        retrieve = get_retriever()
        docs = retrieve("elemen kompetensi instalasi PLTS")
        # -> [{"content": ..., "source": ..., "page": ...}, ...]
    """
    k = top_k or RAG_TOP_K
    collection = get_collection()
    if collection.count() == 0:
        ingest_skkni_docs()  # auto-ingest saat pertama kali dipakai

    embedder = get_embedder()

    def retrieve(query: str) -> List[Dict[str, Any]]:
        query_embedding = embedder.embed_query(query)
        result = collection.query(
            query_embeddings=[query_embedding],
            n_results=min(k, collection.count()) or 1,
            include=["documents", "metadatas", "distances"],
        )
        docs = []
        for text, meta, dist in zip(
            result["documents"][0],
            result["metadatas"][0],
            result["distances"][0],
        ):
            docs.append(
                {
                    "content": text,
                    "source": meta.get("source", "?"),
                    "page": meta.get("page", "?"),
                    # cosine distance -> similarity (1 - distance), untuk info saja
                    "score": 1.0 - float(dist),
                }
            )
        return docs

    return retrieve


def format_context(docs: List[Dict[str, Any]]) -> str:
    """Format hasil retrieve menjadi satu string untuk disuntik ke prompt.

    Contoh output:
        [SKKNI 2019-307.pdf, hal. 12]
        ...teks chunk...
    """
    if not docs:
        return "(Tidak ada referensi SKKNI yang relevan ditemukan.)"
    return "\n\n".join(
        f"[{d['source']}, hal. {d['page']}]\n{d['content']}" for d in docs
    )


def query_skkni(query: str, top_k: int = None) -> str:
    """Helper one-liner: retrieve + format, siap disuntik ke {context} prompt."""
    retrieve = get_retriever(top_k)
    return format_context(retrieve(query))


# ----------------------------------------------------------------------
# Smoke test: python tools/vector_store.py "query anda"
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    ingest_skkni_docs(force="--force" in sys.argv)
    test_query = next((a for a in sys.argv[1:] if not a.startswith("-")), None) or "elemen kompetensi unit skkni"
    print(f'\n=== HASIL RETRIEVE: "{test_query}" ===\n')
    for doc in get_retriever()(test_query):
        print(f"--- [{doc['source']}, hal. {doc['page']}] (score {doc['score']:.3f}) ---")
        print(doc["content"][:300], "...\n")