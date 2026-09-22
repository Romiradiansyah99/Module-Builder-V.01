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

    # Gabung + urutkan by skor
    docs = [d for d in docs if d.get("content", "").strip()]
    docs.sort(key=lambda d: float(d.get("score", 0.0)), reverse=True)
    return docs[:k]
