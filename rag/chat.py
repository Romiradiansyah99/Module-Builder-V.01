"""
CHAT - orchestrator RAG: memori -> rewrite -> retrieve -> konteks -> jawab.
==========================================================================
Dipanggil dari thread worker di server.py (pola SSE sama dgn chat_sync).
Callback:
  status_cb(text, stage)  -> status tahap utk UI
  token_cb(delta)        -> token balasan mengalir (think sudah dibuang)
  sources_cb(docs)       -> sitasi hasil retrieve utk UI (event `sources`)

Jawaban disusun dgn memori percakapan + konteks retrieve; streaming memakai
_raw_chat_stream (bukan langchain invoke) supaya cancel & think-strip jalan.
"""

from typing import Any, Callable, Dict, List

from rag import config
from rag import context as rag_context
from rag import retriever, rewriter, threads


def run_rag_chat(
    thread_id: str,
    message: str,
    *,
    status_cb: Callable[[str, str], None],
    token_cb: Callable[[str], None],
    sources_cb: Callable[[List[Dict[str, Any]]], None],
    context_strategy: str = None,
) -> Dict[str, Any]:
    """Jalankan satu ronde RAG chat. Return {thread_id, answer, sources}."""
    from tools.llm_config import get_llm, get_llm_for_effort, _raw_chat_stream, _ThinkStream

    threads.append_user(thread_id, message)
    transcript = threads.build_transcript(thread_id, message)

    # --- 1) Rewrite (Rewrite-Retrieve-Read), fallback keras di dalam rewriter
    status_cb("Merapikan pertanyaan agar pencarian akurat…", "rag.rewrite")
    llm_low = get_llm_for_effort("low")
    rewritten = rewriter.rewrite_query(llm_low, transcript, message)

    # --- 2) Retrieve gabungan (rag_chat + skkni) + kirim sitasi ke UI
    status_cb("Mencari referensi dokumen paling relevan…", "rag.retrieve")
    docs = retriever.rag_retrieve(rewritten)
    sources = _slim_sources(docs)
    sources_cb(sources)

    # --- 3) Konteks (fast path / create-and-refine / tree-summarization)
    context_text = rag_context.build_context(docs, strategy=context_strategy)

    # --- 4) Jawaban (streaming, Bahasa Indonesia, berbasis konteks)
    llm = get_llm(temperature=config.ANSWER_TEMPERATURE, num_predict=config.ANSWER_MAX_TOKENS)
    system = (
        "Kamu adalah asisten RAG Kemnaker. Jawab pertanyaan user dengan BAHASA INDONESIA "
        "dan HANYA berdasarkan konteks dokumen yang diberikan. Kutip sumber tiap klaim dengan "
        "format [nama file, hal. n]. Bila konteks tidak memuat jawabannya, katakan jujur "
        "bahwa informasi tsb tidak ditemukan di dokumen — jangan berasumsi/mengarang."
    )
    user_prompt = (
        f"{system}\n\nMemori percakapan:\n{transcript}\n\nKonteks dokumen:\n{context_text}\n\n"
        f"Pertanyaan:\n{message}\n\nJawab:"
    )

    visible: List[str] = []
    stripper = _ThinkStream()
    answer = ""

    def on_text(delta: str) -> None:
        vis = stripper.feed(delta)
        if vis:
            visible.append(vis)
            token_cb(vis)

    try:
        resp = _raw_chat_stream(llm, user_prompt, on_text)
        vis = stripper.flush()
        if vis:
            visible.append(vis)
            token_cb(vis)
        answer = "".join(visible).strip()
        if not answer and isinstance(resp.content, str):
            from tools.llm_config import _strip_think

            answer = _strip_think(resp.content).strip()
    except BaseException:
        # cancel/error: jangan simpan asisten palsu; user message tetap tersimpan
        raise

    threads.append_assistant(thread_id, answer)
    return {"thread_id": thread_id, "answer": answer, "sources": sources}


def _slim_sources(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "source": d.get("source", "?"),
            "page": d.get("page", "?"),
            "source_type": d.get("source_type", "upload"),
            "score": round(float(d.get("score", 0.0)), 3),
        }
        for d in docs
    ]
