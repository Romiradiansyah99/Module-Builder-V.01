"""
CONTEXT - rangkum konteks agar muat di budget prompt.
=====================================================
Dua strategi (rag-chatbot):
  * create-and-refine  : dokumen dipotong jadi sub-batch, tiap batch
                         diringkas LLM, hasil digabung "Refined #i:".
  * tree-summarization : dokumen dikelompokkan 2-per-2 ke atas ("pohon"),
                         tiap grup diringkas sampai muat budget.

Keduanya lewat `_summarize` yang sama (get_llm_for_effort("low") +
invoke_with_retry). FALLBACK KERAS: kalau LLM gagal, kembalikan prefix
mentah teks (konteks TIDAK pernah kosong). Jalur cepat: bila teks sudah
di bawah budget, dipakai apa adanya (tanpa panggil LLM).
"""

from typing import Any, Callable, Dict, List

from rag import config


def _summarize(text: str, max_tokens: int = None) -> str:
    """Ringkas `text` sambil menjaga fakta & penanda sumber. Return teks."""
    prompt = (
        "Ringkas teks dokumen teknis berikut menjadi paragraf padat yang mempertahankan "
        "semua fakta penting dan PENANDA SUMBER (format [nama file, hal. n]) tetap utuh. "
        "Jangan menambah/Membuat fakta baru. Balas hanya hasil ringkasan.\n\n" + text
    )
    try:
        from tools.llm_config import get_llm_for_effort, invoke_with_retry

        llm = get_llm_for_effort("low")
        if max_tokens:
            llm.num_predict = max_tokens
        resp = invoke_with_retry(llm, prompt, attempts=2, label="rag.summarize")
        out = (resp.content if isinstance(resp.content, str) else "").strip()
        return out or text
    except Exception as exc:  # noqa: BLE001 - fallback: kembalikan asli
        print(f"[rag] Ringkas gagal (pakai teks asli): {exc}", flush=True)
        return text


def _fmt(docs: List[Dict[str, Any]]) -> str:
    from tools import vector_store as vs

    return vs.format_context(docs)


def _raw_over_budget(docs: List[Dict[str, Any]], budget: int) -> str:
    txt = _fmt(docs)
    return txt[:budget] if len(txt) > budget else txt


def create_and_refine(docs: List[Dict[str, Any]], budget: int) -> str:
    """Bagi dokumen jadi sub-batch muat budget, ringkas tiap batch, gabung."""
    batches: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    cur_tokens = 0
    per_batch = max(1, budget // 2)
    for d in docs:
        approx = _tokenish(d.get("content", ""))
        if cur and cur_tokens + approx > per_batch:
            batches.append(cur)
            cur, cur_tokens = [], 0
        cur.append(d)
        cur_tokens += approx
    if cur:
        batches.append(cur)

    refined = []
    for i, b in enumerate(batches, 1):
        txt = _summarize(_fmt(b))
        refined.append(f"Refined #{i}:\n{txt}")
    return "\n\n".join(refined)[:budget] if _len(refined) > budget else "\n\n".join(refined)


def tree_summarization(docs: List[Dict[str, Any]], budget: int) -> str:
    """Rangkum berjenjang (2-per-grup) sampai muat budget."""
    level: List[Dict[str, Any]] = list(docs)
    guard = 0
    while _tokenish(_fmt(level)) > budget and len(level) > 1 and guard < 12:
        groups = [level[i : i + 2] for i in range(0, len(level), 2)]
        nxt: List[Dict[str, Any]] = []
        for g in groups:
            if len(g) == 1:
                nxt.append(g[0])
                continue
            s = _summarize(_fmt(g))
            # "pseudo-dokumen" meneruskan fakta yang diringkas ke level atas
            nxt.append({**g[0], "content": s, "score": 1.0})
        level = nxt
        guard += 1
    return _raw_over_budget(level, budget)


def _tokenish(text: str) -> int:
    return len(text) // 4


def _len(refined: List[str]) -> int:
    return sum(len(r) for r in refined)


def build_context(docs: List[Dict[str, Any]], strategy: str = None) -> str:
    """Rakit konteks final utk prompt sesuai strategi & budget.

    Jalur cepat bila muat; strategi overflow dikendalikan .env
    (RAG_CONTEXT_STRATEGY): "create-and-refine" | "tree-summarization".
    """
    strategy = (strategy or config.CONTEXT_STRATEGY or "create-and-refine").strip()
    budget = config.CONTEXT_MAX_CHARS
    raw = _fmt(docs)
    if len(raw) <= budget:
        return raw  # muat -> tanpa panggil LLM (cepat & hemat)
    try:
        if strategy == "tree-summarization":
            return tree_summarization(docs, budget)
        return create_and_refine(docs, budget)
    except Exception as exc:  # noqa: BLE001 - fallback keras, jangan pernah kosong
        print(f"[rag] Strategi konteks '{strategy}' gagal - pakai prefix: {exc}", flush=True)
        return raw[:budget]
