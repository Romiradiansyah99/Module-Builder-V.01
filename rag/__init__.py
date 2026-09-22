"""
RAG CHAT - percakapan tanya-jawab atas dokumen (Kemnaker)
==========================================================
Pola Rewrite-Retrieve-Read ala rag-chatbot, diadaptasi ke infra yang sudah
ada: LLM Ollama Cloud (tools/llm_config), embedder (tools/vector_store),
dan vector store Chroma. Menyediakan:

  rag/chat.py        -> orchestrator streaming jawaban (dipakai server.py)
  rag/retriever.py   -> retrieve gabungan (rag_chat + skkni_kemnaker)
  rag/rewriter.py    -> tulis-ulang pertanyaan (Rewrite-Retrieve-Read)
  rag/context.py     -> ciptakan-&-perhalus / rangkuman-pohon (overflow)
  rag/threads.py     -> riwayat percakapan per thread (+ klip token)
  rag/document_manager.py -> upload/list/delete + ingest program
  rag/chunker.py     -> muat PDF/DOCX + potong chunk stabil
  rag/store.py       -> abstraksi VectorStore (Chroma; Qdrant nanti)
"""

__version__ = "0.1.0"
