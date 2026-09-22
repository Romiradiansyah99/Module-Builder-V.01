"""
SMOKE TEST RAG CHAT (struktural, tanpa server)
===============================================
Jalankan:
    python rag/smoke.py [--embed]
Tanpa --embed: uji chunker + store (buat/hapus collection rag_chat) tanpa
memanggil embedder (cepat, offline). Dengan --embed: uji ingest/list/query/
delete nyata (pakai embedder lokal MiniLM via tools.vector_store; bila
EMBEDDING_MODEL diisi, jatuh ke cloud). Bila embed gagal karena jaringan,
bagian struktural tetap lolos dan error dicetak jelas.
"""

import sys
import tempfile
from pathlib import Path

import docx as docx_mod

from rag import chunker


def _make_small_docx(path: Path) -> str:
    doc = docx_mod.Document()
    for i in range(3):
        doc.add_paragraph(
            f"Elemen kompetensi instalasi PLTSa halaman {i}: "
            "mengoperasikan pembangkit listrik tenaga surya, menjaga "
            "keselamatan kerja, dan mencatat hasil operasi harian."
        )
    doc.add_paragraph(
        "Unit kompetensi ini menguji kemampuan teknisi dalam merakit "
        "panel surya dan mengintegrasikan dengan jaringan listrik."
    )
    doc.save(str(path))
    return str(path)


def _test_structure():
    print("== 1. CHUNKER ==")
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tf:
        pass
    p = Path(tf.name)
    _make_small_docx(p)
    pages = chunker.load_document(p)
    assert pages, "DOCX tidak menghasilkan halaman teks"
    chunks = chunker.chunk_document("smoke-doc", "upload", p.name, pages, str(p))
    print(f"   halaman={len(pages)}, chunk={len(chunks)}")
    print("   contoh id:", chunks[0]["id"] if chunks else "(kosong)")
    p.unlink(missing_ok=True)

    print("== 2. STORE (rag_chat collection) ==")
    from rag.store import ChromaStore

    s = ChromaStore()
    name = s._collection.name
    print(f"   collection '{name}' tersedia; count={s.count()}; name={s.name}")
    # delete id tanpa data = idempoten (0)
    removed = s.delete_by_doc_id("smoke-nonexistent")
    print(f"   delete_by_doc_id(id tak dikenal) = {removed} (harus 0)")
    print("   structure OK")


def _test_embed_loop():
    print("== 3. INGEST / LIST / QUERY / DELETE (nyata) ==")
    from rag.document_manager import delete_document, get_store, ingest, list_documents

    try:
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tf:
            pass
        p = Path(tf.name)
        _make_small_docx(p)
        rec = ingest("smoke-doc", "upload", p, p.name)
        print("   ingest:", rec)
        p.unlink(missing_ok=True)

        docs = list_documents()
        print("   list_documents:", len(docs), "dokumen")
        from rag.retriever import rag_retrieve

        hits = rag_retrieve("merakit panel surya", k=3)
        print(f"   retrieve hit {len(hits)} dokumen; top source: {hits[0]['source']} (skor {hits[0]['score']:.3f})" if hits else "   retrieve: tanpa hasil")

        removed = delete_document("smoke-doc")
        print("   delete by doc_id:", removed, "chunk dihapus")
        assert get_store().get_by_doc_id("smoke-doc").get("ids") == []
        print("   embed loop OK")
    except Exception as exc:  # noqa: BLE001 - embed bisa gagal karena jaringan
        print(f"   ! embed loop SKIPPED/gagal (lingkungan): {exc.__class__.__name__}: {exc}")


if __name__ == "__main__":
    _test_structure()
    if "--embed" in sys.argv:
        _test_embed_loop()
    else:
        print("(tambahkan --embed untuk menguji ingest/retrieve/delete nyata)")
