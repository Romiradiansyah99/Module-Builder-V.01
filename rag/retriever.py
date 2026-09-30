"""
RETRIEVER (RAG CHAT) - retrieve gabungan rag_chat + skkni_kemnaker.
==================================================================
Kedua collection memakai embedder yang SAMA (cache `get_embedder()`),
jadi jarak cosine bisa dibandingkan untuk digabung & diurutkan.

SKKNI dibaca read-only via `tools.vector_store.get_retriever()`; rag_chat
(query dokumen user) via Store abstraction. Hasil diberi label source_type.
"""

from typing import Any, Dict, List

from rag import config


def rag_retrieve(query: str, k: int = None) -> List[Dict[str, Any]]:
    """Kembalikan top-k dokumen gabungan, terurut skor menurun.

    Tiap dokumen: {content, source, page, source_type, score}.
    """
    k = k or config.RAG_TOP_K_CHAT
    from tools import vector_store as vs

    embedder = vs.get_embedder()
    query_embedding = embedder.embed_query(query)

    docs: List[Dict[str, Any]] = []

    # 1) Collection rag_chat (dokumen user + program)
    from rag.document_manager import get_store

    store_docs = get_store().query(query_embedding, k=k)
    docs.extend(store_docs)

    # 2) SKKNI readonly (slice lebih kecil agar seimbang dgn dokumen user)
    try:
        skkni_retrieve = vs.get_retriever(top_k=max(1, k // 2))
        skkni_docs = skkni_retrieve(query)
        for d in skkni_docs:
            d["source_type"] = "skkni"
            d.setdefault("page", d.get("page", "?"))
        docs.extend(skkni_docs)
    except Exception as exc:  # noqa: BLE001 - SKKNI gagal jangan menghentikan chat
        print(f"[rag] Retrieve SKKNI gagal (diabaikan): {exc}", flush=True)

    # 3) KB referensi Kemnaker (template + contoh modul). Embedding query
    # dipakai ulang -> tidak ada embed kedua. `source_type` sudah ikut dari
    # metadata Chroma, jadi sitasi UI otomatis berlabel "template" /
    # "contoh_modul" tanpa penandaan manual seperti jalur SKKNI di atas.
    try:
        if config.RAG_TOP_K_KB > 0:
            from rag.kb import kb_retrieve_embedded

            docs.extend(kb_retrieve_embedded(query_embedding, k=config.RAG_TOP_K_KB))
    except Exception as exc:  # noqa: BLE001 - KB gagal jangan menghentikan chat
        print(f"[rag] Retrieve KB referensi gagal (diabaikan): {exc}", flush=True)

    # Gabung + urutkan by skor
    docs = [d for d in docs if d.get("content", "").strip()]
    docs.sort(key=lambda d: float(d.get("score", 0.0)), reverse=True)
    return _ensure_kb_slot(docs[:k], query_embedding, k)


def _ensure_kb_slot(docs: List[Dict[str, Any]], query_embedding, k: int) -> List[Dict[str, Any]]:
    """Jaminan slot KB (template) SETELAH penggabungan, bukan hanya di dalam KB.

    `rag.kb.kb_retrieve_embedded()` sudah menyisipkan satu slot
    ENSURE_SOURCE_TYPE bila belum terambil, tetapi jaminan itu bisa hilang di
    sini: tiga korpus digabung lalu dipotong `docs[:k]`, sementara skor
    template selalu rendah (korpusnya hanya 8 chunk ber-`{{ tag }}` melawan
    contoh modul yang berprosa asli) - jadi ia jatuh persis ketika korpus lain
    mengisi semua slot.

    Terukur 2026-09-30 (k=6/RAG_TOP_K_CHAT), "bagian wajib dan struktur modul
    pelatihan Kemnaker" -> konteks akhir {skkni 3, contoh_modul 1, program 2}
    TANPA template, padahal justru pertanyaan struktur/gaya modul yang
    membutuhkannya; dua query lain kebetulan masih kebagian slot. Tanpa ini
    kalimat "template dijamin terpakai" di README/DEPLOY hanya berlaku di
    lapisan KB, tidak di konteks yang benar-benar dibaca LLM.

    Slot diambil dengan mengganti dokumen ber-skor TERENDAH (bukan menambah),
    supaya panjang hasil tetap `k` dan peringkat sisanya tidak bergeser.
    """
    try:
        if k < 2 or config.RAG_TOP_K_KB <= 0:
            # k=1: seluruh konteks hanya satu dokumen - memaksakan template di
            # situ akan MENGHAPUS dokumen yang paling relevan, kebalikan dari
            # maksud jaminan ini.
            return docs
        from rag.kb import ENSURE_SOURCE_TYPE, get_kb_store

        if not ENSURE_SOURCE_TYPE:
            return docs
        if any(d.get("source_type") == ENSURE_SOURCE_TYPE for d in docs):
            return docs
        extra = [
            d for d in get_kb_store().query(
                query_embedding, k=1, where={"source_type": ENSURE_SOURCE_TYPE})
            if str(d.get("content", "")).strip()
        ]
        if not extra:
            return docs
        return docs + extra if len(docs) < k else docs[: k - 1] + extra
    except Exception as exc:  # noqa: BLE001 - jaminan KB jangan mematikan chat
        print(f"[rag] Jaminan slot KB gagal (diabaikan): {exc}", flush=True)
        return docs
