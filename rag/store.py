"""
VECTOR STORE (RAG CHAT) - abstraksi + implementasi Chroma.
================================================================
Sengaja dipisah dari `tools/vector_store.py` (yang tetap dipakai Agent 1
agar store SKKNI tak berubah). Hanya RAG chat yang lewat sini; bila nanti
migrasi ke Qdrant, implementasi cukup diganti tanpa menyentuh pemanggil.

Abstraksi VectorStore:
  upsert(ids, documents, metadatas, embeddings)
  delete_by_doc_id(doc_id) -> int (jumlah chunk terhapus)
  query(embedding, k) -> "docs" (teks + metadata + skor)
  count() -> int
  get_grouped() -> dict doc_id -> {metadata, chunk_count}
"""

import logging
from typing import Any, Dict, List

from rag import config

logging.getLogger("pypdf").setLevel(logging.ERROR)


class VectorStore:
    """Interface vektor. Subkelas wajib mengimplementasi metode di bawah."""

    name = "abstract"

    def upsert(self, ids, documents, metadatas, embeddings) -> None:  # noqa: D401
        raise NotImplementedError

    def delete_by_doc_id(self, doc_id: str) -> int:
        raise NotImplementedError

    def query(self, embedding: List[float], k: int) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def count(self) -> int:
        raise NotImplementedError

    def get_grouped(self) -> Dict[str, Dict[str, Any]]:
        raise NotImplementedError


class ChromaStore(VectorStore):
    """ChromaDB persistent, collection rag_chat (cosine HNSW)."""

    name = "chroma"

    def __init__(self) -> None:
        import chromadb

        db_dir = config.chroma_db_dir()
        db_dir.parent.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(db_dir))
        self._collection = self._client.get_or_create_collection(
            name=config.CHAT_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )

    # internal helper (dipakai juga pemanggil utk menghitung sebelum delete)
    def get_by_doc_id(self, doc_id: str) -> Dict[str, Any]:
        return self._collection.get(where={"doc_id": doc_id})

    def upsert(self, ids, documents, metadatas, embeddings) -> None:
        self._collection.upsert(
            ids=list(ids),
            documents=list(documents),
            metadatas=list(metadatas),
            embeddings=embeddings,
        )

    def delete_by_doc_id(self, doc_id: str) -> int:
        data = self.get_by_doc_id(doc_id)
        ids = data.get("ids") or []
        if ids:
            self._collection.delete(ids=ids)
        return len(ids)

    def query(self, embedding: List[float], k: int) -> List[Dict[str, Any]]:
        n_total = self._collection.count()
        if n_total == 0:
            return []
        result = self._collection.query(
            query_embeddings=[embedding],
            n_results=min(k, n_total),
            include=["documents", "metadatas", "distances"],
        )
        out = []
        for text, meta, dist in zip(
            result.get("documents", [[]])[0] or [],
            result.get("metadatas", [[]])[0] or [],
            result.get("distances", [[]])[0] or [],
        ):
            out.append(
                {
                    "content": text,
                    "source": (meta or {}).get("source", "?"),
                    "page": (meta or {}).get("page", "?"),
                    "source_type": (meta or {}).get("source_type", "upload"),
                    # cosine distance -> similarity
                    "score": 1.0 - float(dist),
                }
            )
        return out

    def count(self) -> int:
        return self._collection.count()

    def get_grouped(self) -> Dict[str, Dict[str, Any]]:
        data = self._collection.get(include=["metadatas"])
        grouped: Dict[str, Dict[str, Any]] = {}
        metadatas = data.get("metadatas") or []
        for meta in metadatas:
            meta = meta or {}
            doc_id = meta.get("doc_id", "?")
            doc = grouped.setdefault(doc_id, {
                "doc_id": doc_id,
                "source": meta.get("source", "?"),
                "source_type": meta.get("source_type", "upload"),
                "path": meta.get("path", ""),
                "ingested_at": meta.get("ingested_at", ""),
                "page_count": set(),
                "chunk_count": 0,
            })
            page = meta.get("page", "1")
            doc["page_count"].add(page)
            doc["chunk_count"] += 1
            # ambil path/ingested_at pertama yang ada
            if meta.get("path") and not doc["path"]:
                doc["path"] = meta["path"]
            if meta.get("ingested_at") and not doc["ingested_at"]:
                doc["ingested_at"] = meta["ingested_at"]
        for doc in grouped.values():
            doc["page_count"] = len(doc["page_count"])
        return grouped
