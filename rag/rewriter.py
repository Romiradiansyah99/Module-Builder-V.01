"""
REWRITER - Rewrite-Retrieve-Read: tulis-ulang pertanyaan utk retrieval.
=======================================================================
Pertanyaan user tidak selalu optimal utk dicari di vektor. LLM menyusun
ulang jadi pertanyaan mandiri (berdasar riwayat percakapan) yang menangkap
niat sebenarnya. HARD FALLBACK: bila LLM gagal/kosong, pakai pesan asli
(chat tidak boleh mati hanya karena rewrite salah).

Rewrite memakai effort "low" (model chat ringan, hemat biaya).
"""

import re


def _clean(raw: str) -> str:
    raw = (raw or "").strip().strip('"').strip("“”")
    raw = re.sub(r"\s+", " ", raw)
    return raw


def rewrite_query(llm, history_transcript: str, message: str) -> str:
    """Tulis-ulang `message` jadi pertanyaan pencarian mandiri. Return str."""
    prompt = (
        "Kamu adalah perangkai pertanyaan untuk mesin pencari dokumen teknis Kemnaker "
        "(modul/SKKNI). Ubah pesan user berikut menjadi SATU pertanyaan mandiri, "
        "jelas, dan mudah dicari, dalam Bahasa Indonesia. Sertakan konteks dari "
        "riwayat bila perlu, tapi jangan menambahkan fakta di luar pesan. "
        "Balas HANYA dengan teks pertanyaan, tanpa awalan/penjelasan.\n\n"
        + (f"Riwayat percakapan:\n{history_transcript}\n\n" if history_transcript.strip() else "")
        + f"Pesan user:\n{message}"
    )
    try:
        from tools.llm_config import invoke_with_retry

        resp = invoke_with_retry(llm, prompt, attempts=2, label="rag.rewrite")
        cleaned = _clean(resp.content if isinstance(resp.content, str) else "")
        if cleaned:
            return cleaned
    except Exception as exc:  # noqa: BLE001 - fallback keras ke pesan asli
        print(f"[rag] Rewrite gagal (pakai pesan asli): {exc}", flush=True)
    return message
