"""
AGENT 3 - EVALUATOR (The Strict QA)
===================================
Mengecek draf Agent 2 dengan .with_structured_output() Pydantic (wajib
sesuai PRD). Loop revisi maksimal MAX_ITERATIONS; jika iterasi ke-3 masih
FAIL, dipaksakan PASS dengan status "PASS (Need Human Review)".
"""

import json
import re
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from agents.agent1_syllabus import PARTICIPANT_BRIEF
from agents.agent2_content import _subbab_title_violations
from agents.state import MAX_ITERATIONS, STATUS_NEED_HUMAN_REVIEW, STATUS_FAIL, ModuleState
from tools.llm_config import auto_effort, get_llm_for_effort, invoke_with_retry
from tools.word_injector import ROW_KEYS


def _extract_verdict_json(text: str) -> dict:
    """Ambil objek JSON pertama dari output LLM (tahan markdown fence)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"Output tidak berisi JSON: {text[:150]}...")
    return json.loads(text[start : end + 1])


class EvaluationResult(BaseModel):
    """Skema structured output Agent 3 (wajib .with_structured_output())."""
    status_evaluasi: Literal["PASS", "FAIL"] = Field(
        description="PASS jika draf memenuhi SEMUA aturan wajib, selain itu FAIL."
    )
    feedback: str = Field(
        description="Jika FAIL: penjelasan DETAIL bagian mana yang harus diubah Agent 2. Jika PASS: kosongkan."
    )


EVALUATION_PROMPT = """Anda adalah Inspektur Mutu Kemnaker yang sangat kaku. Evaluasi draf modul berdasarkan silabus berikut:

{participants}

=== SILABUS MODUL ===
{syllabus}

=== DRAF MODUL (draft_json dari Agent 2) ===
{draft}

Aturan Wajib Evaluasi:
1. Harus ada bagian pengantar.
2. Konten harus detail (TIDAK BOLEH berupa bullet points singkat) - tiap bagian harus multi-paragraf dan mudah dipahami pemula.
3. Judul subbab pada pengetahuan_content harus bernomor ("1. ...", "1.1 ...") dan redaksinya sesuai EKSPLISIT dengan elemen/pengetahuan silabus.
4. Bahasa harus ramah ORANG AWAM (lihat konteks peserta di atas): istilah teknis baru diperkenalkan dengan penjelasan; jika ada bagian yang menyebut peserta sudah berpengalaman atau memakai jargon tanpa penjelasan, FAIL dan minta diperbaiki.
5. PASANGAN VERBA: verba pasif pada indikator silabus harus berpasangan dengan verba aktif berakar sama di pengetahuan/keterampilan (contoh: "teridentifikasi" -> "mengidentifikasi"); jika tidak konsisten, FAIL dan minta dikoreksi.

Catatan penting: baris "...(DIPOTONG - konten di tengah ADA dan dianggap lengkap)..." berarti konten di tengah key ADA. Kehadiran blok Mermaid, kelengkapan key, dan kelengkapan row-list (elemen_rows/bahan_rows/cek_observasi_rows/cek_hasil_rows/kamus_rows/referensi_rows) TIDAK perlu Anda nilai (sudah dicek otomatis). JANGAN menjadikan bagian yang tidak terlihat sebagai alasan FAIL - nilai hanya teks yang terlihat.

Jika melanggar, berikan status 'FAIL' dan tuliskan di 'feedback' bagian mana yang harus diubah Agent 2. Jika sesuai, berikan 'PASS' dan kosongkan feedback."""


def _summarize_draft(draft_json: dict, max_chars: int = 45000) -> str:
    """Draft terlalu panjang untuk satu prompt? Potong per-key secara adil.

    Ronde 10: potongan HANYA bagian TENGAH — awal & akhir tiap key selalu
    tampil. Akar bug "Agent 2/3 selalu gagal": potongan lama memotong ekor
    key besar sehingga blok ```mermaid (biasanya di akhir pengetahuan_content)
    TIDAK PERNAH terlihat evaluator -> FAIL palsu berulang. Sekarang evaluator
    juga diberi tahu bahwa bagian bertanda DIPOTONG dianggap ADA.
    """
    parts = []
    budget = max(2000, max_chars // max(1, len(draft_json)))
    for key, value in draft_json.items():
        text = str(value)
        if len(text) > budget:
            head = text[: max(1200, budget - 1200)]
            tail = text[-1000:]
            text = head + "\n...(DIPOTONG - konten di tengah ADA dan dianggap lengkap)...\n" + tail
        parts.append(f'--- key "{key}" ---\n{text}')
    return "\n\n".join(parts)


def _deterministic_violations(draft_json: dict,
                              syllabus_rows: Optional[List[dict]] = None) -> list:
    """Ronde 10: aturan yang bisa dicek PROGRAMATIS dievaluasi di sini (0 token)
    — bukan oleh LLM. Mermaid & kelengkapan key bersifat binary: pemeriksaan
    LLM di atas draf terpotong hanya menghasilkan FAIL palsu berulang.

    Ronde 11: tambahan - mermaid WAJIB di dalam pengetahuan_content (bukan
    key lain, agar benar-benar jadi gambar di docx), lik_gambar_kerja juga
    wajib berisi blok mermaid (gambar kerja LIK), dan kelima row-list LIK/
    lampiran wajib terisi (tabel bahan/cek/kamus/referensi pada template
    resmi 2024 berbentuk loop baris).

    Ronde 12 (16 komentar reviewer): SATU blok mermaid per subbab elemen
    (K0/K9), judul subbab PERSIS redaksi silabus (K10/K11), dan evaluasi
    pengetahuan/praktik berformat daftar "1) ..." (K18).
    """
    violations = []
    pengetahuan = str(draft_json.get("pengetahuan_content") or "")
    if "```mermaid" not in pengetahuan:
        violations.append(
            'Draf tidak memuat blok skrip Mermaid. Sisipkan blok ```mermaid ... ``` '
            'DI DALAM nilai key "pengetahuan_content" (letakkan di bagian akhir), '
            "berisi flowchart alur kerja untuk pembaca awam."
        )
    lik_gambar = str(draft_json.get("lik_gambar_kerja") or "")
    if "```mermaid" not in lik_gambar:
        violations.append(
            'Key "lik_gambar_kerja" wajib berisi SATU blok ```mermaid ... ``` '
            "berisi flowchart alur pekerjaan LIK (Mulai -> langkah kerja -> "
            "Selesai) sesuai skenario praktik."
        )
    empty = sorted(k for k, v in draft_json.items() if not str(v).strip() and k not in ROW_KEYS)
    if empty:
        violations.append(f"Key template masih kosong: {', '.join(empty)} - isi seluruhnya.")
    missing_rows = [k for k in ROW_KEYS if not draft_json.get(k)]
    if missing_rows:
        violations.append(
            f"Row-list wajib belum terisi: {', '.join(missing_rows)} - masing-masing "
            "harus berisi LIST OF OBJECTS minimal sesuai aturan (bahan_rows>=3, "
            "cek_observasi_rows>=3, cek_hasil_rows>=3, kamus_rows>=3, referensi_rows>=2) "
            "dengan key yang ditentukan."
        )
    if syllabus_rows:
        # K0/K9: SATU diagram mermaid per subbab elemen pengetahuan.
        elemen_no = sorted({
            str(r.get("elemen_no", "")).strip().rstrip(".")
            for r in syllabus_rows if str(r.get("elemen_no", "") or "").strip()
        })
        n_elemen = len([e for e in elemen_no if e.isdigit()]) or 1
        n_mermaid = pengetahuan.count("```mermaid")
        if n_mermaid < n_elemen:
            violations.append(
                f"pengetahuan_content hanya memuat {n_mermaid} blok ```mermaid "
                f"sedangkan modul punya {n_elemen} elemen. WAJIB SATU diagram "
                "per subbab elemen (\"1. ...\", \"2. ...\", ...) - letakkan di "
                "akhir bagian subbab masing-masing, flowchart alur kerja "
                "elemen tersebut (maksimal 8 kotak)."
            )
        # K10/K11: judul subbab PERSIS redaksi silabus (pemindai bersama Agent 2).
        violations.extend(_subbab_title_violations(pengetahuan, syllabus_rows))
        # Ronde 13: foto internet butuh query dari Agent 2 - cover + tiap subbab.
        if not str(draft_json.get("cover_image_query") or "").strip():
            violations.append(
                'Key "cover_image_query" kosong - tulis SATU query pencarian '
                "gambar bahasa Inggris yang paling mewakili topik modul untuk "
                "foto COVER PAGE (mis. \"steam power plant surface condenser\")."
            )
        iq = draft_json.get("image_queries")
        n_iq = len(iq) if isinstance(iq, list) else 0
        if n_iq < n_elemen:
            violations.append(
                f'Key "image_queries" hanya berisi {n_iq} objek, harus SATU PER '
                f"SUBBAB ELEMEN ({n_elemen} elemen). Bentuk: [{{\"subbab\": \"1\", "
                '"query": "<kata kunci gambar bahasa Inggris sesuai isi subbab>", '
                '"judul": "<judul gambar bahasa Indonesia untuk caption>"}], ...].'
            )
        # K18: evaluasi pengetahuan/praktik = daftar bernomor "1) ...".
        for key, pola in (
            ("evaluasi_pengetahuan", 'daftar bernomor "1) Pengetahuan tentang '
                                     '<butir pengetahuan silabus>" urut per elemen'),
            ("evaluasi_praktik", 'daftar bernomor "1) <keterampilan>" urut per elemen'),
        ):
            value = str(draft_json.get(key) or "").strip()
            if not value or not re.match(r"^\s*1\)\s", value):
                violations.append(f'Key "{key}" WAJIB berisi {pola}.')
    return violations


def agent3_node(module: ModuleState) -> dict:
    """QA node: evaluasi draft_json -> status PASS/FAIL + feedback.

    Input: ModuleState (payload Send API dari Agent 2).
    Output: partial update GlobalState -> {"modules": [modul + hasil evaluasi]}.
    """
    draft_json = module.get("draft_json") or {}
    if not draft_json:
        # Tidak ada draf = FAIL otomatis dengan feedback jelas.
        verdict = EvaluationResult(status_evaluasi="FAIL", feedback="Draf kosong - Agent 2 harus membuat ulang seluruh konten modul.")
    else:
        # Ronde 10: aturan binary (mermaid, key kosong) dicek programatis lebih
        # dulu - gagal di sini langsung FAIL tanpa membakar token evaluator,
        # dan kebal terhadap pemotongan teks yang membuat LLM salah melihat.
        # Ronde 12: bawa syllabus_rows (judul subbab/mermaid/evaluasi).
        det = _deterministic_violations(draft_json, module.get("syllabus_rows"))
        if det:
            verdict = EvaluationResult(status_evaluasi="FAIL", feedback=" ".join(det))
        else:
            # Mode AUTO (ronde 3): evaluator pakai effort "medium" (kaku, output
            # verdict pendek - tidak butuh budget besar).
            llm = get_llm_for_effort(auto_effort("agent3"))
            prompt = EVALUATION_PROMPT.format(
                syllabus=module["syllabus_content"],
                draft=_summarize_draft(draft_json),
                participants=PARTICIPANT_BRIEF,
            )
            try:
                # Jalur utama (PRD 4C): .with_structured_output dengan skema Pydantic.
                # attempts=1: glm cloud KONSISTEN mengabaikan json_schema (pakai key
                # "status") - retry 3x hanya membuang waktu sebelum fallback.
                evaluator = llm.with_structured_output(EvaluationResult)
                verdict = invoke_with_retry(evaluator, prompt, attempts=1, label="agent3")
            except Exception:
                # Fallback terbukti: glm cloud mengabaikan format json_schema dan
                # menjawab prosa / key alias. Minta ulang JSON mentah lalu validasi
                # manual. Feedback dibatasi agar JSON tidak terpotong budget token.
                strict = prompt + (
                    "\n\nWAJIB: jawab HANYA satu objek JSON tanpa teks lain, "
                    'format persis: {"status_evaluasi": "PASS" atau "FAIL", "feedback": "..."}'
                    "\nBatas feedback: MAKSIMAL 3 kalimat ringkas."
                )
                data = None
                for attempt in range(2):
                    resp = invoke_with_retry(llm, strict, label="agent3")
                    try:
                        data = _extract_verdict_json(resp.content)
                        break
                    except (ValueError, json.JSONDecodeError):
                        # Coba perbaiki JSON terpotong (tutup object yang belum selesai)
                        text = resp.content.strip()
                        text = text[text.find("{"):] if "{" in text else text
                        try:
                            data = json.loads(text.rstrip().rstrip(",") + '"}')
                        except Exception:
                            strict += "\nJawaban Anda sebelumnya terpotong - buat feedback LEBIH PENDEK."
                            continue
                if data is None:
                    # Verdict konservatif daripada mematikan worker: anggap FAIL
                    # (loop revisi tetap berjalan / iterasi ke-3 paksa human review).
                    print("[agent3] JSON verdict tak terbaca 2x - paksa FAIL generik")
                    data = {"status_evaluasi": "FAIL",
                            "feedback": "Evaluasi tidak selesai (output LLM terpotong). Perbaiki kelengkapan draf."}
                # glm cloud menomori skema: pakai "status" bukan "status_evaluasi" ->
                # alias diterima agar verdict tetap tervalidasi Pydantic.
                if "status_evaluasi" not in data:
                    for alias in ("status", "verdict", "hasil"):
                        if alias in data:
                            data["status_evaluasi"] = data.pop(alias)
                            break
                verdict = EvaluationResult(**data)

    iteration = module.get("iteration_count", 0) + 1

    # Aturan PRD: iterasi ke-3 masih FAIL -> paksa PASS + "Need Human Review"
    if verdict.status_evaluasi == STATUS_FAIL and iteration >= MAX_ITERATIONS:
        status = STATUS_NEED_HUMAN_REVIEW
        feedback = (verdict.feedback or "").strip()
    elif verdict.status_evaluasi == STATUS_FAIL:
        status = STATUS_FAIL
        feedback = verdict.feedback
    else:
        status = "PASS"
        feedback = ""

    updated = {
        **module,
        "status_evaluasi": status,
        "evaluator_feedback": feedback,
        "iteration_count": iteration,
    }
    return {"modules": [updated]}