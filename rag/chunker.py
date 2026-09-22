"""
CHUNKER - muat PDF/DOCX lalu potong jadi chunk stabil RAG chat.
================================================================
Memakai ulang splitter & loader yang sama dengan vector_store (Agent 1),
sehingga style chunking konsisten. Chunk id stabil `{doc_id}::p{page}::c{idx}`
> upsert idempoten & delete-per-doc cukup filter metadata.
"""

import hashlib
from pathlib import Path
from typing import Any, Dict, List

from langchain_text_splitters import RecursiveCharacterTextSplitter

from rag import config
from tools import vector_store as vs


def make_splitter():
    """Splitter identik dgn vector_store (CHUNK_SIZE / CHUNK_OVERLAP)."""
    return RecursiveCharacterTextSplitter(
        chunk_size=vs.CHUNK_SIZE,
        chunk_overlap=vs.CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " ", ""],
    )


def doc_id_for_bytes(data: bytes) -> str:
    """Id dokumen upload = sha1 dari byte file (dedupe file sama)."""
    return hashlib.sha1(data).hexdigest()


def load_document(path: Path) -> List[Dict[str, str]]:
    """Kembalikan halaman teks {page, text, source} dari PDF atau DOCX.

    PDF: reuse `_load_pdf_pages` (pypdf). DOCX: paragraf + tabel via
    doc_utils (python-docx); DOCX tak punya nomor halaman -> label "1".
    """
    path = Path(path)
    ext = path.suffix.lower()
    pages: List[Dict[str, str]] = []

    if ext == ".pdf":
        pages = vs._load_pdf_pages(path)
        return pages if pages else _dummy_page(path)

    if ext == ".docx":
        from tools.doc_utils import extract_docx_text

        text = extract_docx_text(path)
        return [({"page": "1", "text": text, "source": path.name})] if text else []

    raise ValueError(f"Format tak didukung: {ext} (hanya .pdf dan .docx)")


def _dummy_page(path: Path) -> List[Dict[str, str]]:
    return [{"page": "1", "text": "", "source": path.name}]


def chunk_document(
    doc_id: str, source_type: str, source: str, pages: List[Dict[str, str]], path: str = ""
) -> List[Dict[str, Any]]:
    """Potong halaman -> daftar chunk {id, text, metadata}.

    chunk id = `{doc_id}::p{page}::c{idx}`. metadata berisi doc_id, source,
    source_type, page, chunk_index, path (lokasi file di disk utk delete),
    ingested_at (timestamp utk urutan daftar).
    """
    splitter = make_splitter()
    chunks: List[Dict[str, Any]] = []
    for page in pages:
        for idx, chunk in enumerate(splitter.split_text(page["text"])):
            if not chunk.strip():
                continue
            pid = str(page["page"])
            chunks.append(
                {
                    "id": f"{doc_id}::p{pid}::c{idx}",
                    "text": chunk,
                    "metadata": {
                        "doc_id": doc_id,
                        "source": source,
                        "source_type": source_type,
                        "page": pid,
                        "chunk_index": str(idx),
                        "path": path,
                        "ingested_at": str(_now_ts()),
                    },
                }
            )
    return chunks


def text_len(text: str) -> int:
    """Estimasi token: 1 token ~ 4 karakter (cukup utk klip konteks)."""
    return len(text) // 4


def _now_ts() -> int:
    import time

    return int(time.time())
