"""
AGENT 1 - SYLLABUS CREATOR (dig LLM murni -> build LLM)
=======================================================
Workflow (ronde 3):
  Tahap 1 (dig)    : PERCAKAPAN LLM MURNI ala konsultan kurikulum - menggali
                     kebutuhan user (audiens, tujuan, cakupan, alokasi),
                     menjawab pertanyaan user tentang program, bebas
                     berdialog sampai user bilang "lanjut". Effort LOW
                     (murah). Fallback: teks deterministik _ask_reply().
  Tahap 2 (build)  : SATU modul per unit terpilih; module_title = judul unit
                     kompetensi PERSIS dari daftar unit program; silabus
                     per unit diambil dari tabel acuan program docx
                     (deterministik, via tools/unit_extractor.py); LLM hanya
                     mengisi bagian kosong / unit tanpa tabel acuan.
                     Effort HIGH (EXTRA bila program draft).

Node graph: menerima GlobalState, mengisi `modules`, lalu graph PAUSE
(interrupt) sebelum Map_Modules menunggu approval manusia (HITL).
Gerbang build: regex intent (hemat - tanpa LLM) ATAU keputusan LLM dig
(mode "build"); balasan dig pertama bertanda SCOPE_MARKER.
"""

import json
import re
from typing import List, Optional, Tuple

from agents.state import GlobalState, ModuleState, make_module
from tools.ai_memory import get_style_block
from tools.doc_utils import get_program_text, number_pengetahuan, to_active_voice
from tools.llm_config import auto_effort, get_llm_for_effort, invoke_with_retry
from tools.unit_extractor import (
    extract_program_meta,
    extract_program_units,
    extract_unit_syllabus_rows,
    format_units_markdown,
    get_units_block,
    match_unit,
    rows_status,
)
from tools.vector_store import format_context, get_retriever

# Penanda tersembunyi pada balasan tahap "dig" - tak tampak di UI (markdown
# comment) tapi terbaca oleh _scope_answered() pada giliran berikutnya.
SCOPE_MARKER = "<!-- SCOPE_ASKED -->"

# Jawaban user yang langsung dianggap pilihan cakupan / perintah membangun.
# "lanjut/lanjutkan" ditambah ronde 3: user boleh berdialog bebas lalu bilang
# "lanjut" untuk langsung membangun. ("bikin/buat" sengaja TIDAK dianggap
# intent - kalimat pembuka "aku mau bikin modul" harus tetap masuk percakapan
# dig, bukan langsung build tanpa cakupan.)
ASK_INTENT_RE = re.compile(
    r"\b\d+\.\d+\b|semua\s+(unit|modul)|seluruh\s+unit|kelompok\s+(inti|pilihan)"
    r"|\blanjut(kan)?\b",
    re.I,
)

# Ronde 8: setelah silabus pernah dibuat, permintaan REVISI juga masuk build
# (bukan dig) - "ubah durasi jadi..." tetap membangun ulang tanpa babak baru.
REVISION_RE = re.compile(
    r"\b(ubah|revisi|ganti|perbaiki|perbaik|tambah(kan)?|kurang(i|kan)?|hapus)"
    r".{0,40}\b(durasi|jp|silabus|indikator|pengetahuan|keterampilan|unit|modul|elemen|kuk)\b",
    re.I,
)

# Untuk program DRAFT (tanpa daftar unit): jawaban setuju pun dianggap pilihan
ASK_INTENT_DRAFT_RE = re.compile(
    r"^(ya|ok|oke|baik|silakan|boleh|gas|lanjut|usulkan|usul saja|buat saja)\b|"
    r"\b(ya|ok|oke|silakan|usulkan)\b.*\b(unit|usul|susun|buat)\b",
    re.I,
)

_ROW_PROMPT_KEYS = ("elemen_no", "elemen", "kuk_no", "kuk", "indikator",
                    "pengetahuan", "keterampilan", "durasi")

HISTORY_CAP = 12  # pesan terakhir yang dimasukkan ke prompt (anti bloat)

# Konteks peserta (ronde 6, PENTING): user sudah menegaskan peserta pelatihan
# adalah orang awam - semua agent WAJIB membawa blok ini; Agent 1 dilarang
# menanyakan "siapa pesertanya" lagi (jawabannya sudah diketahui).
PARTICIPANT_BRIEF = """=== KONTEKS PESERTA PELATIHAN (PENTING - BERLAKU UNTUK SEMUA TULISAN) ===
Peserta pelatihan ini adalah ORANG AWAM / pemula di bidang materi program ini -
bukan tenaga kerja yang sudah berpengalaman. Konsekuensi wajib:
- Bahasa sederhana dan ramah pemula; setiap istilah teknis diperkenalkan dari nol.
- Contoh kasus memakai situasi sehari-hari yang dikenal orang awam.
- Langkah kerja diuraikan pelan-pelan, seolah pembaca baru pertama kali mengenal bidang ini.
- JANGAN bertanya lagi kepada user tentang latar belakang peserta - sudah diketahui."""

SYSTEM_PROMPT = """Anda adalah Ahli Kurikulum Kemnaker. Program pelatihan dan daftar unit kompetensinya sudah diekstraksi otomatis - DAFTAR UNIT di bawah adalah SUMBER KEBENARAN (jangan mengarang judul/kode unit).

{style}
{participants}
=== PROGRAM PELATIHAN TERPILIH ===
{program}

=== DAFTAR UNIT KOMPETENSI (EKSTRAKSI OTOMATIS - SUMBER KEBENARAN) ===
{units_block}

=== TABEL ACUAN SILABUS PER UNIT (dari dokumen program) ===
{ref_tables}

=== REFERENSI RAG SKKNI ===
{context}

=== RIWAYAT PERCAKAPAN ===
{history}

Tugas Anda menjalankan dua mode:

1. mode "ask": user BELUM memilih cakupan (belum menjawab "semua unit" atau menyebut nomor unit). Kembalikan modules: [] dan tulis `reply` berupa kalimat pembuka + daftar unit yang bisa dipilih + pertanyaan "Mau dibuatkan semua unit kompetensi atau beberapa unit saja?".

2. mode "build": user SUDAH memilih cakupan ("semua", nomor unit seperti 1.1/2.3, kelompok, judul unit, atau "ya" untuk program draft). Buat TEPAT satu modul per unit terpilih dengan ketentuan:
   - PROGRAM DRAFT (DAFTAR UNIT kosong): usulkan sendiri daftar unit kompetensi yang relevan dari isi program; module_title = usulan Anda; isi syllabus_rows penuh dari referensi SKKNI; tentukan alokasi_waktu sendiri ("N JP @ 45 menit").
   - "module_title" WAJIB identik karakter-per-karakter dengan kolom Judul Unit Kompetensi pada daftar di atas (dilarang meringkas, menerjemahkan, atau mengganti).
   - Sertakan "kode_unit" dan "alokasi_waktu" dari daftar (biarkan kosong bila daftar kosong).
   - Untuk unit yang tabel acuannya "lengkap": rows diambil otomatis dari tabel acuan. Namun BILA Anda melihat sel kosong (indikator/pengetahuan/keterampilan), WAJIB sertakan "syllabus_rows" untuk baris kuk_no yang masih kosong tersebut - sistem akan menggabungkan tanpa menimpa nilai yang sudah ada.
   - Untuk unit yang tabel acuannya "kerangka (perlu dilengkapi)": isi "syllabus_rows" untuk SETIAP kuk_no yang tercantum pada tabel acuan, mengisi bagian kosong (indikator/pengetahuan/keterampilan) dari referensi SKKNI; salin elemen/kuk/durasi yang sudah ada apa adanya.
   - Untuk unit yang tabel acuannya "kerangka (perlu dilengkapi)": isi "syllabus_rows" untuk SETIAP kuk_no yang tercantum pada tabel acuan, mengisi bagian kosong (indikator/pengetahuan/keterampilan) dari referensi SKKNI; salin elemen/kuk/durasi yang sudah ada apa adanya.
   - Untuk unit "(belum tersedia - susun dari referensi SKKNI)": susun "syllabus_rows" penuh dari referensi RAG SKKNI: elemen kompetensi + KUK + indikator + pengetahuan + keterampilan & sikap + durasi (JP) yang masuk akal untuk unit tsb.
   - Tulis `reply` ringkas: konfirmasi modul yang dibuat.
   - Jika user bertanya hal umum (bukan pilihan cakupan), jawab dengan mode "ask" tanpa membuat modul.

Format syllabus_rows (list of object, key wajib persis):
{{"elemen_no": "1.", "elemen": "...", "kuk_no": "1.1", "kuk": "...", "indikator": "...", "pengetahuan": "...", "keterampilan": "...", "durasi": "3 JP"}}

ATURAN ALOKASI JP (Anda adalah ahli kurikulum - angka JP wajib masuk akal):
- 1 JP = 45 menit pelajaran. Durasi per baris KUK lazim 2-4 JP (minimal 1, maksimal 6); KUK yang ringkas 1-2 JP, KUK berat/praktik lapangan 4-6 JP.
- Kolom "pengetahuan" WAJIB bernomor urut mulai "1." (format: "Penjelasan tentang:\n1. ...\n2. ...").
- Kolom "pengetahuan" WAJIB memakai kata kerja AKTIF (meN-/ber-, mis. "mencatat", "menganalisis", "menerapkan", "memilih"). Verba pasif di-/ter- yang Anda salin dari kolom "indikator" HARUS dikonversi ke bentuk aktifnya (indikator "dicatat" -> pengetahuan "mencatat"; "diterapkan" -> "menerapkan"). DILARANG memakai kata pasif berawalan "ter-" pada kolom pengetahuan; verba pasif "di-" hanya untuk kolom "indikator".
- PASANGAN VERBA PER BARIS KUK (WAJIB - contoh nyata program operasi kondenser): setiap verba pasif pada "indikator" HARUS muncul dalam bentuk AKTIF berakar sama pada "pengetahuan" dan "keterampilan" baris yang sama. Contoh: indikator "Teridentifikasinya dasar, tujuan, perintah kerja, perlengkapan K2..." -> keterampilan "mengidentifikasi maksud dan tujuan pengoperasian Kondenser secara disiplin dan cermat..."; indikator "dicatat" -> pengetahuan "mencatat". Bentuk pasif "-nya" (mis. "teridentifikasinya") menjadi verba aktif murni ("mengidentifikasi").
- TOTAL durasi semua baris KUK dalam satu modul WAJIB sama dengan alokasi_waktu unit (sistem akan mengoreksi otomatis bila meleset - usahakan sudah tepat).
- Jika alokasi_waktu unit kosong pada daftar, tetapkan sendiri total yang masuk akal (jumlah KUK x 2-4 JP, dibulatkan ke kelipatan terdekat yang wajar) dan tulis di "alokasi_waktu" sebagai "N JP @ 45 menit".

ATURAN OUTPUT (WAJIB - jawab HANYA JSON valid, tanpa teks lain, tanpa markdown fence):
{{
  "mode": "ask" atau "build",
  "program_name": "<nama program pelatihan>",
  "reply": "<teks balasan percakapan untuk user, markdown ringkas>",
  "modules": [
    {{
      "module_title": "<identik dengan daftar unit>",
      "kode_unit": "<kode unit>",
      "alokasi_waktu": "<perkiraan waktu>",
      "syllabus_rows": [<daftar objek sesuai format di atas>]
    }}
  ]
}}

Ingat: silabus hanya dilanjutkan ke Agent 2 setelah user meng-approve."""


def _extract_json(text: str) -> dict:
    """Ambil objek JSON pertama dari output LLM (tahan-banting terhadap
    markdown fence / teks sampingan)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"Output LLM tidak berisi JSON: {text[:200]}...")
    return json.loads(text[start : end + 1])


def rows_to_markdown(rows: List[dict]) -> str:
    """syllabus_rows -> markdown pipe table (kontrak lama syllabus_content,
    tetap dipakai prompt Agent 2/3 dan smoke test)."""
    header = "| Elemen Kompetensi | KUK | Indikator | Pengetahuan | Keterampilan dan Sikap | Durasi |"
    sep = "|---|---|---|---|---|---|"
    lines = [header, sep]
    for r in rows or []:
        vals = [
            f"{r.get('elemen_no', '')} {r.get('elemen', '')}".strip(),
            f"{r.get('kuk_no', '')} {r.get('kuk', '')}".strip(),
            r.get("indikator", "") or "",
            r.get("pengetahuan", "") or "",
            r.get("keterampilan", "") or "",
            r.get("durasi", "") or "",
        ]
        lines.append("| " + " | ".join(v.replace("\n", "; ").replace("|", "/") for v in vals) + " |")
    return "\n".join(lines)


def _normalize_rows(rows) -> List[dict]:
    """Rows dari LLM/docx -> dict bersih dengan semua key wajib terisi string.
    Kolom pengetahuan dipaksa bernomor urut "1. ", "2. ", ... (feedback ronde 2)."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        clean = {k: str(r.get(k, "") or "").strip() for k in _ROW_PROMPT_KEYS}
        # Ronde 6: indikator ikut dikirim - verba pasif di-/ter- yang disalin
        # dari indikator dikonversi ke aktif; WAJIB di pengetahuan DAN
        # keterampilan (pasangan verba per baris KUK harus konsisten).
        ind = clean.get("indikator", "")
        clean["pengetahuan"] = number_pengetahuan(clean["pengetahuan"], ind)
        clean["keterampilan"] = to_active_voice(clean["keterampilan"], ind)
        out.append(clean)
    return out


# "16 JP", "16 JP @ 45 menit", "3 jp" - ambil angka JP pertama di teks
_JP_RE = re.compile(r"(\d+)\s*JP", re.I)


def _distribute_jp(total: int, jp: List[int]) -> List[int]:
    """Bagi `total` JP ke tiap baris KUK proporsional terhadap bobot `jp`
    (min 1 JP per baris, sisa pembulatan diberikan ke baris yang paling
    undershoot). Ahli JP: durasi per KUK lazim 2-4 JP - bobot asli dihormati,
    yang dikoreksi hanya TOTAL-nya."""
    n = len(jp)
    if n == 0 or sum(jp) == 0:
        return [max(1, round(total / n))] * n if n else []
    if total < n:  # alokasi tak cukup utk min 1 JP/baris - pathological
        return [1] * n
    s = total / sum(jp)
    new = [max(1, round(v * s)) for v in jp]
    diff = total - sum(new)
    while diff > 0:  # kurang -> isi baris yang paling undershoot
        i = min(range(n), key=lambda i: (new[i] - jp[i] * s, new[i]))
        new[i] += 1
        diff -= 1
    while diff < 0:  # lebih -> pangkas baris yang paling overshoot (min 1)
        i = max(range(n), key=lambda i: (new[i] - jp[i] * s, new[i]))
        if new[i] <= 1:
            break
        new[i] -= 1
        diff += 1
    return new


def _apply_jp_rules(rows: List[dict], alokasi: str) -> Tuple[List[dict], str]:
    """Aturan ahli JP (feedback ronde 2 item 5, deterministik):
    1. 1 JP = 45 menit; alokasi unit selalu dinormalisasi ke "N JP @ 45 menit".
    2. Total durasi semua baris KUK WAJIB sama dengan alokasi unit - diukur di
       data nyata, tabel acuan program SENDIRI tidak konsisten (unit 1.1:
       alokasi 16 JP tapi jumlah durasi baris 42 JP), jadi total di-enforce.
    3. Durasi kosong default 3 JP sebelum distribusi; alokasi kosong/tanpa
       angka diisi dari jumlah baris.
    Return (rows, alokasi_final); rows yang sudah konsisten tak diubah."""
    if not rows:
        return rows, alokasi
    jp = []
    for r in rows:
        m = _JP_RE.search(r.get("durasi") or "")
        jp.append(int(m.group(1)) if m else 3)  # kosong -> default 3 JP
    m = _JP_RE.search(alokasi or "")
    total = int(m.group(1)) if m else None
    if total and total > 0:
        if sum(jp) != total:
            jp = _distribute_jp(total, jp)
    else:
        total = sum(jp)
    for r, v in zip(rows, jp):
        r["durasi"] = f"{v} JP"
    return rows, f"{total} JP @ 45 menit"


def _scope_answered(history: List[dict], allow_affirmative: bool = False) -> bool:
    """True jika agent sudah pernah bertanya (marker) DAN user membalas
    dengan jawaban cakupan (nomor unit / 'semua' / kelompok).

    allow_affirmative=True dipakai saat program DRAFT tanpa daftar unit -
    di situ pertanyaannya "mau saya usulkan unitnya?" sehingga jawaban
    "ya/ok/silakan" juga sah sebagai pilihan cakupan."""
    asked = any(
        m.get("role") == "assistant" and SCOPE_MARKER in (m.get("content") or "")
        for m in history
    )
    if not asked:
        return False
    intent = ASK_INTENT_DRAFT_RE if allow_affirmative else ASK_INTENT_RE
    return any(
        intent.search(m.get("content") or "")
        for m in history if m.get("role") == "user"
    )


def _ask_reply(program_title: str, units: List[dict]) -> str:
    if not units:
        # Program draft: tidak ada daftar unit untuk dipilih.
        return (
            f"{SCOPE_MARKER}\n"
            f"Halo! Saya sudah membaca program pelatihan **{program_title or 'Anda'}**, "
            "tapi belum menemukan daftar unit kompetensi di dalamnya (kemungkinan "
            "program ini masih draft).\n\n"
            "Mau saya **usulkan unit kompetensinya** dari isi program dan referensi "
            "SKKNI? Balas **ya** untuk melanjutkan, atau ceritakan dulu unit apa saja "
            "yang Anda inginkan."
        )
    lines = [
        f"Halo! Saya sudah membaca program pelatihan **{program_title or 'pelatihan'}** "
        f"yang berisi **{len(units)} unit kompetensi** berikut:",
        "",
        format_units_markdown(units),
        "",
        "Mau dibuatkan **semua unit kompetensi**, atau **beberapa unit saja**? "
        "Sebutkan nomornya (mis. `1.1` dan `2.1`) atau ketik `semua`.",
    ]
    return f"{SCOPE_MARKER}\n" + "\n".join(lines)


DIG_PROMPT = """Anda adalah Konsultan Kurikulum Kemnaker yang ramah dan berpengalaman. Anda sedang mengobrol dengan calon penyelenggara pelatihan untuk menggali kebutuhannya SEBELUM menyusun silabus.

{participants}

=== PROGRAM YANG SEDANG DIBACA ===
{program_brief}

=== DAFTAR UNIT KOMPETENSI PROGRAM ===
{units_brief}

=== RIWAYAT PERCAKAPAN ===
{history}

{style}
CARA MEMBALAS (seperti manusia, bukan robot):
1. JAWAB DULU apa yang user tanyakan/katakan secara wajar berdasarkan info di atas - jangan mengarang data di luar konteks program.
2. Lalu, bila kebutuhan masih mengambang, ajukan MAKSIMAL 2 pertanyaan penggalian yang paling penting: tujuan pelatihan, unit yang diutamakan, alokasi waktu, atau NAMA & PROFESI penyusun modul (data ini wajib dikumpulkan sebelum modul dibangun - dicantumkan pada halaman daftar nama penyusun). Jangan mengulang pertanyaan yang sudah terjawab. DILARANG bertanya "siapa peserta pelatihannya" - peserta sudah diketahui orang awam (lihat konteks di atas).
3. Ringkas dan hangat: 2-5 kalimat, bahasa Indonesia santai-formal. Jangan tampilkan seluruh daftar unit lagi bila sudah pernah ditampilkan.
4. INISIATIF proaktif: bila ada unit dengan tabel acuan "BELUM LENGKAP" atau "belum ada", beri tahu user secara singkat dan TAWARKAN mengisi sendiri dari referensi SKKNI (contoh: "Indikator unit 1.1 masih kosong - mau saya isi sendiri dari SKKNI, atau memang tidak perlu?"). Jangan tunggu user menyadari kekosongannya.
5. Pilih mode "build" HANYA bila user sudah jelas meminta dibuatkan (mis. "lanjut", "buat semua unit", menyebut nomor unit, atau menyetujui tawaran pengisian) ATAU kebutuhannya sudah lengkap dan menyuruh Anda memulai. Selain itu mode "dig".
{dig_note}
{first_note}
ATURAN OUTPUT (WAJIB - jawab HANYA satu objek JSON, tanpa teks lain):
{{"mode": "dig" atau "build", "reply": "<teks balasan percakapan>"}}"""


def _dig_turn(program_title: str, units: List[dict], history: List[dict]) -> Tuple[str, bool]:
    """Satu giliran percakapan penggalian kebutuhan (LLM MURNI, effort LOW).

    Return (reply, llm_build):
    - reply  : teks balasan konsultan untuk user (kosong bila LLM minta build)
    - llm_build : True bila LLM menilai kebutuhan sudah jelas -> langsung build

    Fallback: LLM gagal -> teks deterministik _ask_reply() (sistem tak mati).
    """
    if units:
        program_brief = f"{program_title or '(program tanpa judul)'} - FINAL, {len(units)} unit kompetensi."
        # Ronde 5: status tabel acuan per unit - bahan inisiatif menawarkan
        # pengisian sel kosong dari SKKNI.
        note = {"complete": "tabel acuan lengkap",
                "partial": "tabel acuan BELUM LENGKAP (indikator/pengetahuan ada yang kosong)",
                "none": "belum ada tabel acuan"}
        units_brief = "\n".join(f"- {u['no']} {u['judul']} [{note[rows_status(u['no'])]}]" for u in units)
    else:
        program_brief = (
            f"{program_title or '(program tanpa judul)'} - DRAFT: belum ada tabel daftar unit "
            "maupun silabus di dalamnya."
        )
        units_brief = "(belum ada daftar unit - Anda bisa mengusulkan unit kompetensi dari isi program)"

    history_text = "\n".join(
        f"{'USER' if m.get('role') == 'user' else 'ASSISTANT'}: {m['content']}"
        for m in history[-HISTORY_CAP:]
    ) or "(belum ada percakapan)"

    # Auto-wrap: kalau sudah lama menggali, ajak user memutuskan.
    dig_count = sum(1 for m in history if m.get("role") == "assistant")
    dig_note = (
        "5. Anda SUDAH menggali beberapa giliran - sekarang ajak user memutuskan: "
        "tawarkan membangun SELURUH unit sebagai asumsi terbaik, atau sebutkan unit pilihannya."
        if dig_count >= 3 else ""
    )
    # Ronde 6: giliran PERTAMA jangan mengasumsikan program yang sudah
    # terpasang akan dipakai - user bisa membawa program yang benar-benar baru.
    # Ronde 12 (K20): data penyusun WAJIB diminta SEJAK giliran pertama -
    # dicantumkan pada halaman "Daftar Nama Penyusun" modul.
    first_note = ""
    if dig_count == 0:
        first_note = (
            "6. GILIRAN PERTAMA: sapa user secara NETRAL dan JANGAN langsung mengasumsikan "
            "bahwa program di atas akan dipakai - dia mungkin membawa program pelatihan yang "
            "sama sekali baru. Tanyakan dulu program apa yang mau dibuatkan modulnya, lalu "
            "tawarkan pilihan: memakai program yang sudah tersedia di sistem, atau upload "
            "program miliknya sendiri. Jangan menyebut judul program sebagai keputusan final. "
            "SEKALIGUS tanyakan NAMA & PROFESI penyusun modul (mis. 'Boleh tahu siapa yang "
            "akan tercantum sebagai penyusun modul ini - nama dan profesinya?'), karena "
            "data itu wajib ada sebelum modul dibangun."
        )

    prompt = DIG_PROMPT.format(
        program_brief=program_brief,
        units_brief=units_brief,
        history=history_text,
        dig_note=dig_note,
        first_note=first_note,
        style=get_style_block(),
        participants=PARTICIPANT_BRIEF,
    )

    llm = get_llm_for_effort(auto_effort("agent1.dig"))
    try:
        response = invoke_with_retry(llm, prompt, label="agent1.dig")
        data = _extract_json(response.content)
        mode = str(data.get("mode") or "dig").strip().lower()
        if mode == "build":
            return "", True
        reply = str(data.get("reply") or "").strip()
        if reply:
            # Tandai giliran dig PERTAMA (state + _scope_answered + strip UI).
            asked_already = any(
                m.get("role") == "assistant" and SCOPE_MARKER in (m.get("content") or "")
                for m in history
            )
            if not asked_already:
                reply = f"{SCOPE_MARKER}\n" + reply
            return reply, False
    except Exception as exc:  # noqa: BLE001 - fallback deterministik selalu ada
        print(f"[agent1] dig LLM gagal ({type(exc).__name__}: {exc}) -> fallback deterministik")
    return _ask_reply(program_title, units), False


def parse_agent1_json(text: str) -> dict:
    """Parse output LLM tahap build -> dict {'program_name','reply','modules'}."""
    data = _extract_json(text)
    program_name = str(data.get("program_name", "")).strip()
    reply = str(data.get("reply", "")).strip()
    raw = data.get("modules", []) or []
    if not isinstance(raw, list):
        raise ValueError("Field 'modules' bukan list.")
    return {"program_name": program_name, "reply": reply, "raw_modules": raw}


_PENYUSUN_PROMPT = """Dari RIWAYAT PERCAKAPAN berikut, ekstrak data PENYUSUN modul yang disebut user (nama dan profesi - mis. instruktur, dosen, teknisi). RIWAYAT PERCAKAPAN:
{history}

ATURAN OUTPUT (WAJIB - jawab HANYA satu objek JSON tanpa teks lain):
{{"nama": "<nama penyusun pertama>", "profesi": "<profesi penyusun pertama>", "nama2": "<nama penyusun kedua bila ada>", "profesi2": "<profesi penyusun kedua bila ada>"}}
Isi string kosong "" bila data tidak pernah disebut dalam percakapan - JANGAN mengarang."""


def _extract_penyusun(history: List[dict]) -> dict:
    """Ronde 12 (K20): tarik data penyusun dari percakapan penggalian
    (Agent 1 menanyakanya sejak giliran pertama). Gagal LLM -> dict kosong
    (Agent 2 menulis "-" pada tabel penyusun, tidak pernah crash)."""
    history_text = "\n".join(
        f"{'USER' if m.get('role') == 'user' else 'ASSISTANT'}: {m.get('content', '')}"
        for m in history[-HISTORY_CAP:]
    )
    if not history_text.strip():
        return {}
    try:
        llm = get_llm_for_effort(auto_effort("agent1.penyusun"))
        resp = invoke_with_retry(llm, _PENYUSUN_PROMPT.format(history=history_text),
                                 label="agent1.penyusun")
        data = _extract_json(resp.content)
    except Exception as exc:  # noqa: BLE001 - penyusun kosong, bukan fatal
        print(f"[agent1] ekstraksi penyusun gagal ({type(exc).__name__}) - dikosongkan")
        return {}
    out = {}
    for key in ("nama", "profesi", "nama2", "profesi2"):
        value = str(data.get(key) or "").strip()
        if value and value.lower() not in ("null", "none", "-"):
            out[key] = value
    return out


def parse_modules_json(text: str) -> tuple:
    """Back-compat: (program_name, List[ModuleState]) dari output build."""
    data = parse_agent1_json(text)
    modules = _finalize_modules(data["raw_modules"], data["program_name"], None)
    if not modules:
        raise ValueError("Output LLM tidak berisi modul apa pun.")
    return data["program_name"], modules


# Kolom isi silabus yang boleh diisi dari LLM bila sel docx KOSONG. Nilai docx
# yang sudah terisi SELALU menang (tidak tergantikan). Key identitas
# (elemen_no/kuk_no) tidak ikut - mereka penanda baris, bukan isi.
_MERGE_FILL_KEYS = ("elemen", "kuk", "indikator", "pengetahuan", "keterampilan",
                    "durasi")


def _merge_rows(base_rows: List[dict], llm_rows: List[dict]) -> List[dict]:
    """Ronde 5 (diperluas Ronde 17): isi sel KOSONG di rows tabel acuan docx
    dengan nilai dari rows LLM. Dua jaminan:
      1. TERISI - SEMUA kolom isi (elemen, kuk, indikator, pengetahuan,
         keterampilan, durasi) yang kosong diisi dari LLM; sebelumnya hanya 3
         kolom konten, sehingga elemen/kuk/durasi bisa tertinggal kosong.
      2. TIDAK TERGANTIKAN - nilai docx yang sudah terisi SELALU menang; LLM
         hanya melengkapi sel yang benar-benar kosong.
    Pemetaan utama per kuk_no; bila kuk_no tidak cocok, fallback posisional
    (baris LLM ke-i untuk baris docx ke-i) agar partial unit tetap terisi.
    Key identitas (elemen_no/kuk_no) tidak pernah diganti."""
    if not llm_rows:
        return base_rows
    by_kuk = {}
    for r in llm_rows:
        key = str(r.get("kuk_no", "")).strip()
        if key and key not in by_kuk:
            by_kuk[key] = r
    out = []
    for i, r in enumerate(base_rows):
        r = dict(r)
        src = by_kuk.get(str(r.get("kuk_no", "")).strip())
        if src is None and i < len(llm_rows):
            src = llm_rows[i]  # fallback posisional utk kuk_no yang tak cocok
        if isinstance(src, dict):
            for k in _MERGE_FILL_KEYS:
                if not str(r.get(k, "") or "").strip() and str(src.get(k, "") or "").strip():
                    r[k] = src[k]
        out.append(r)
    return out


def _finalize_modules(
    raw_modules: List[dict],
    program_name: str,
    units: Optional[List[dict]],
    penyusun: Optional[dict] = None,
) -> List[ModuleState]:
    """Post-processing deterministik: snap judul ke daftar unit, isi kode/
    alokasi, rows dari tabel acuan menang atas rows LLM, derive markdown."""
    if units is None:
        units = list(extract_program_units())
    docx_rows = extract_unit_syllabus_rows()

    modules: List[ModuleState] = []
    seen_units = set()
    unit_no_by_idx: List[str] = []  # urutan no unit sejajar `modules`
    for item in raw_modules:
        if not isinstance(item, dict):
            continue
        title = str(item.get("module_title", "")).strip()
        if not title:
            continue
        unit = match_unit(units, title)
        no = ""
        if unit:
            no = unit["no"]
            if no in seen_units:
                continue  # satu modul per unit
            seen_units.add(no)
            title = unit["judul"]  # snap ke kanonik
            kode = str(item.get("kode_unit", "")).strip() or unit["kode"]
            alokasi = str(item.get("alokasi_waktu", "")).strip() or unit["alokasi"]
            if rows_status(no) in ("complete", "partial"):
                # Ronde 5: rows docx tetap sumber utama, tapi sel kosongnya
                # diisi dari rows LLM (merge per kuk_no) - bila LLM mengirim.
                rows = _merge_rows(
                    _normalize_rows(docx_rows.get(no)),
                    _normalize_rows(item.get("syllabus_rows")),
                )
            else:
                rows = _normalize_rows(item.get("syllabus_rows"))
        else:
            kode = str(item.get("kode_unit", "")).strip()
            alokasi = str(item.get("alokasi_waktu", "")).strip()
            rows = _normalize_rows(item.get("syllabus_rows"))

        # Ahli JP: enforce total durasi = alokasi unit + normalisasi alokasi.
        rows, alokasi = _apply_jp_rules(rows, alokasi)

        if rows:
            syllabus = rows_to_markdown(rows)
        else:
            syllabus = str(item.get("syllabus_content", "")).strip()
        if not syllabus:
            continue
        unit_no_by_idx.append(no)
        modules.append(make_module(
            module_id="",  # diisi setelah sort
            module_title=title,
            syllabus_content=syllabus,
            syllabus_rows=rows,
            kode_unit=kode,
            alokasi_waktu=alokasi,
            penyusun=penyusun,
        ))

    # Urutkan sesuai urutan daftar unit agar M01, M02, ... konsisten
    order = {u["no"]: i for i, u in enumerate(units)}
    paired = sorted(zip(modules, unit_no_by_idx), key=lambda p: order.get(p[1], 999))
    modules = [m for m, _ in paired]
    for i, m in enumerate(modules, start=1):
        m["module_id"] = f"M{i:02d}"
    return modules


def agent1_node(state: GlobalState) -> dict:
    """Node LangGraph: tahap dig (LLM murni, effort low) atau tahap build (LLM+RAG)."""
    history = state.get("chat_history", [])
    last_user = next((m["content"] for m in reversed(history) if m.get("role") == "user"), "")

    units = list(extract_program_units())
    program_title = extract_program_meta().get("judul", "")

    # --- Gerbang build: regex dulu (murah), baru keputusan LLM dig ---
    build_intent = ASK_INTENT_RE.search(last_user) or (
        not units and ASK_INTENT_DRAFT_RE.search(last_user)
    )
    scope_done = _scope_answered(history, allow_affirmative=not units)
    # Ronde 8: begitu silabus SUDAH pernah dibuat, gerbang "scope_done"
    # tidak boleh memaksa SEMUA chat berikutnya masuk build (sumber error
    # "tidak menghasilkan modul apa pun" saat user sekadar ngobrol). Chat
    # berikutnya kembali lewat dig - LLM yang memutuskan ask/build, kecuali
    # pesannya jelas intent build atau permintaan revisi.
    already_built = bool(state.get("modules"))
    revision_intent = bool(REVISION_RE.search(last_user)) if already_built else False

    # --- TAHAP 1 (dig): percakapan penggalian kebutuhan, LLM MURNI ---
    if not build_intent and not (scope_done and not already_built) and not revision_intent:
        dig_reply, llm_build = _dig_turn(program_title, units, history)
        if not llm_build:
            return {
                "modules": [],
                "program_name": program_title,
                "chat_history": [{"role": "assistant", "content": dig_reply}],
            }
        # LLM dig menilai kebutuhan sudah jelas -> lanjut ke tahap build.

    # --- TAHAP 2 (build): cakupan sudah dipilih ---
    # RAG umum dari pesan user terakhir
    retrieve = get_retriever()
    rag_docs = retrieve(last_user) if last_user else []
    context = format_context(rag_docs)

    # Referensi per unit yang butuh LLM (partial / none) - RAG per unit
    ref_blocks = []
    for u in units:
        st = rows_status(u["no"])
        if st == "complete":
            continue
        if st == "partial":
            rows = extract_unit_syllabus_rows().get(u["no"], ())
            ref_blocks.append(
                f"UNIT {u['no']} - {u['judul']} (kode {u['kode']}) - tabel acuan KERANGKA, "
                f"lengkapi bagian kosong untuk tiap kuk_no berikut:\n{_format_rows_block(rows)}"
            )
        else:
            docs = retrieve(f"{u['judul']} {u['kode']} elemen kompetensi kriteria unjuk kerja") if u["kode"] else []
            ref_blocks.append(
                f"UNIT {u['no']} - {u['judul']} (kode {u['kode'] or '-'}) - tabel acuan "
                f"(belum tersedia - susun dari referensi SKKNI). Referensi RAG:\n{format_context(docs)}"
            )

    program = get_program_text() or "(Program pelatihan belum dipilih.)"
    history_text = "\n".join(
        f"{'USER' if m.get('role') == 'user' else 'ASSISTANT'}: {m['content']}"
        for m in history[-HISTORY_CAP:]
    ) or "(belum ada percakapan)"

    prompt = SYSTEM_PROMPT.format(
        program=program,
        style=get_style_block(),
        participants=PARTICIPANT_BRIEF,
        units_block=get_units_block(units),
        ref_tables="\n\n".join(ref_blocks) or (
            "(program draft tanpa tabel acuan - susun seluruh unit dan silabus "
            "dari isi program serta referensi SKKNI di bawah.)"
            if not units else
            "(semua unit terpilih sudah punya tabel acuan lengkap - tidak ada yang perlu Anda isi.)"
        ),
        context=context,
        history=history_text,
    )

    # Mode AUTO (ronde 3): effort build = high, atau extra bila program draft
    # (harus mengusulkan unit sendiri - butuh "berpikir" lebih banyak).
    llm = get_llm_for_effort(auto_effort("agent1.build", draft=not units))

    # Retry 2x: glm kadang membungkus JSON dengan teks/naratif.
    last_err = None
    parsed = None
    for attempt in range(2):
        response = invoke_with_retry(llm, prompt, label="agent1.build")
        try:
            parsed = parse_agent1_json(response.content)
            break
        except ValueError as exc:
            last_err = exc
            prompt = (
                f"{prompt}\n\n"
                f"PERINGATAN: jawaban Anda sebelumnya tidak valid ({exc}). "
                f"Jawab ULANG dengan JSON valid saja."
            )
    if parsed is None:
        # Ronde 8: jangan matikan percakapan dengan error - laporkan anggun,
        # user tetap bisa chat (kendala teknis dicatat di log server).
        print(f"[agent1] build JSON gagal: {last_err}")
        return {
            "modules": [],
            "program_name": program_title,
            "chat_history": [{"role": "assistant", "content":
                "Maaf, saya terkendala teknis sesaat saat merangkai silabus. "
                "Coba kirim ulang permintaan Anda — atau ceritakan dulu mau "
                "dibuatkan modul untuk unit yang mana."}],
        }

    # Ronde 12 (K20): data penyusun diekstrak dari percakapan lalu dibawa
    # ke tiap modul (Agent 2 memasangkannya ke tabel Daftar Nama Penyusun).
    penyusun = _extract_penyusun(history)
    modules = _finalize_modules(parsed["raw_modules"], parsed["program_name"], units,
                                penyusun=penyusun)
    if not modules:
        # Ronde 8: user sedang ngobrol / bertanya hal lain, BUKAN memilih
        # cakupan - JANGAN error. Balas sebagai percakapan biasa (reply LLM
        # bila ada, else satu giliran dig).
        reply = parsed["reply"].strip()
        if not reply:
            dig_reply, _ = _dig_turn(program_title, units, history)
            reply = dig_reply
        print("[agent1] build tanpa modul (user tidak meminta cakupan) -> dibalas sebagai percakapan")
        return {
            "modules": [],
            "program_name": program_title,
            "chat_history": [{"role": "assistant", "content": reply}],
        }

    program_name = parsed["program_name"] or program_title

    # Balasan chat: pakai reply LLM bila ada, else ringkasan deterministik
    reply = parsed["reply"].strip()
    if not reply:
        summary_lines = [
            f"Saya sudah menyusun draft silabus untuk **{program_name}** dengan {len(modules)} modul:"
        ]
        for m in modules:
            summary_lines.append(f"- **{m['module_id']} — {m['module_title']}**")
        summary_lines.append(
            "\nSilakan tinjau tabel silabus di bawah. Klik **Approve** untuk mulai produksi, atau tulis revisi Anda."
        )
        reply = "\n".join(summary_lines)

    return {
        "modules": modules,
        "program_name": program_name,
        "chat_history": [{"role": "assistant", "content": reply}],
    }


def _format_rows_block(rows) -> str:
    lines = ["| elemen_no | elemen | kuk_no | kuk | indikator | pengetahuan | keterampilan | durasi |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        vals = [str(r.get(k, "") or "").replace("\n", "; ").replace("|", "/") for k in _ROW_PROMPT_KEYS]
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)