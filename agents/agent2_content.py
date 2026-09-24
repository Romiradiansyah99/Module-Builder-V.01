"""
AGENT 2 - CONTENT CREATOR (The Worker)
=====================================
Menerima SATU ModuleState (dikirim via Send API saat fan-out) dan
mengubah silabus menjadi draf modul lengkap dalam bentuk draft_json
yang siap di-inject ke template docx via docxtpl.

Jika ada evaluator_feedback -> lakukan REVISI draf sebelumnya.
"""

import difflib
import json
import re
from typing import List

from agents.state import ModuleState
from tools.word_injector import IMAGE_QUERY_KEYS, ROW_KEYS
from agents.agent1_syllabus import PARTICIPANT_BRIEF
from tools.doc_utils import (
    _LIST_ITEM_RE,
    get_example_module_text,
    get_template_tags,
    number_pengetahuan,
    pengetahuan_items as _pengetahuan_items,
    to_active_voice,
)
from tools.llm_config import auto_effort, get_llm_for_effort, invoke_with_retry

SYSTEM_PROMPT = """Anda adalah Penulis Modul Vokasi Kemnaker. Tugas Anda adalah mengembangkan silabus menjadi materi yang detail, praktikal, dan ramah pemula, sesuai dengan contoh modul dan template modul yang telah diberikan.

{participants}

=== IDENTITAS MODUL (data resmi - WAJIB disalin PERSIS, jangan mengarang) ===
judul_modul: {module_title}
kode_unit: {kode_unit}
alokasi_waktu: {alokasi_waktu}
nama_penyusun: {nama_penyusun}
profesi_penyusun: {profesi_penyusun}

=== SILABUS MODUL (WAJIB DIPATUHI - ini bahan audit tim Kemnaker) ===
{syllabus}

=== TEMPLATE TAG YANG HARUS DIISI (key JSON WAJIB persis seperti ini) ===
{tags}

=== CONTOH MODUL (acuan gaya, struktur, & level of detail) ===
{example}

{feedback_section}

ATURAN OUTPUT (WAJIB):
1. Jawab HANYA satu objek JSON valid tanpa markdown fence dan tanpa teks lain.
2. Key JSON harus PERSIS daftar template tag di atas. Setiap key berisi konten teks panjang (multi-paragraf) untuk bagian template tersebut. Pindah paragraf ditulis sebagai newline \\n.
3. Key ROW-LIST (LIST OF OBJECTS, bukan teks):
   - "elemen_rows" — SATU object PER BARIS tabel silabus, key WAJIB: "elemen_no" (nomor elemen, mis. "1."), "elemen" (nama elemen kompetensi DENGAN nomor di depan, mis. "1. Membuat dokumen"), "kuk_no" (mis. "1.1"), "kuk" (kriteria unjuk kerja dari silabus), "indikator" (indikator unjuk kerja dari silabus), "pengetahuan" (pengetahuan terkait dari silabus — WAJIB dipertahankan penomorannya persis seperti silabus, mis. "Penjelasan tentang:\\n1. ...\\n2. ..."), "keterampilan" (keterampilan & sikap kerja terkait), "durasi" (alokasi waktu dari silabus, mis. "2 JP"). Jumlah baris = jumlah KUK silabus, urut per elemen. Kolom "pengetahuan" LANGSUNG daftar bernomor - DILARANG menambah kalimat pembuka ("Peserta mampu...", "Materi mencakup...") dan DILARANG mengulang butir yang sudah muncul di baris lain.
   - "bahan_rows" — key: "no", "nama" (nama barang), "spek" (spesifikasi), "jumlah". Isi bahan/alat bahan praktik sesuai skenario kerja unit ini (minimal 3 baris).
   - "cek_observasi_rows" — key: "langkah" (prosedur/langkah kerja yang dinilai - SATU langkah per baris, diambil dari langkah kerja/elemen), "acuan" (acuan pembanding, mis. "SOP", "Manual Book", "Prosedur kerja"). Minimal 3 baris.
   - "cek_hasil_rows" — key: "no", "aspek" (aspek hasil kerja yang dinilai), "standar" (standar keberterimaan yang terukur). Minimal 3 baris.
   - "kamus_rows" — key: "no" ("a.", "b.", ...), "istilah" (istilah teknis dari modul), "arti" (penjelasan singkat). Minimal 3 baris.
   - "referensi_rows" — key: "no" ("a.", "b.", ...), "url" (sumber referensi: judul buku/URL). Minimal 2 baris.
   Contoh: {{"elemen_rows": [{{"elemen_no": "1.", "elemen": "1. Membuat dokumen", "kuk_no": "1.1", "kuk": "Aplikasi dibuka sesuai prosedur", "indikator": "Aplikasi terbuka", "pengetahuan": "Penjelasan tentang:\\n1. Perangkat lunak yang dipakai\\n2. Prosedur pembuatan dokumen", "keterampilan": "Membuka aplikasi sesuai prosedur", "durasi": "2 JP"}}]}}
4. Key "judul_modul", "kode_unit", "alokasi_waktu" WAJIB PERSIS seperti bagian IDENTITAS MODUL di atas (jika data kosong, tulis "-"). "nama_penyusun" dan "profesi_penyusun" WAJIB PERSIS data penyusun di atas (tulis "-" bila kosong); "nama_penyusun_2" dan "profesi_penyusun_2" diisi "-" bila tidak ada penyusun kedua. "unit_kompetensi" diisi judul unit pada silabus, "bentuk_pelatihan" diisi "Luring", "lik_nama" diisi nama pekerjaan praktik, "lik_nomor" diisi "1/1". Key "deskripsi_unit" WAJIB PENDEK (1 kalimat, maksimal 200 karakter) dengan pola "Terimplementasinya <judul unit> sesuai prosedur kerja dan standar yang berlaku." - DILARANG menulis kalimat "Peserta mampu..." atau "Materi mencakup..." di dalamnya.
5. STRUKTUR "pengetahuan_content" (WAJIB meniru contoh modul - gaya buku teks):
   - Setiap elemen silabus menjadi SUBBAB: baris judul di awal baris dengan format "1. <redaksi elemen silabus PERSIS karakter-per-karakter>" lalu sub-subbab "1.1 <redaksi butir pengetahuan silabus PERSIS>" untuk tiap butir pengetahuan.
   - Di BAWAH tiap judul, tulis penjelasan multi-paragraf SANGAT DETAIL (2-5 paragraf, masing-masing 3-6 kalimat) untuk pembaca awam: definisi, langkah praktis, contoh nyata di dunia kerja, tips.
   - Judul subbab HARUS PERSIS redaksi elemen/pengetahuan silabus - bahan audit tim Kemnaker; sistem menimpa judul yang tidak persis, jadi tulis persis dari awal.
   - Baris judul TIDAK boleh digabung dengan paragraf penjelasan (pisahkan \\n).
6. Konten harus SANGAT DETAIL, bukan bullet points singkat. Pembaca adalah orang yang sangat awam. Untuk bagian pendukung (kamus, referensi, penilaian) cukup ringkas dan padat.
7. WAJIB menyertakan diagram Mermaid - SATU blok untuk SETIAP subbab elemen ("1. ...", "2. ...") di dalam nilai key "pengetahuan_content" (letakkan di akhir bagian subbab tsb), DAN satu blok lagi di key "lik_gambar_kerja" (gambar kerja alur pekerjaan LIK). Bentuk blok:
```mermaid
flowchart TD
A[Mulai] --> B[Merencanakan pekerjaan]
B --> C[Menyiapkan sarana]
C --> D[Melaksanakan pekerjaan]
D --> E[Membuat laporan]
```
Sesuaikan isi kotak flowchart dengan langkah kerja subbab/elemen silabus modul ini (maksimal 8 kotak per diagram). DEFAULT-kan "flowchart LR" (alur berurutan mengalir ke samping - lebar penuh halaman, terbaca jelas); "flowchart TD" hanya untuk alur bercabang/percobaan keputusan. Hanya gunakan sintaks: baris "flowchart LR"/"flowchart TD", kotak "A[Label]", panah "-->", cabang "B{{Pertanyaan?}}" bila perlu. JANGAN menulis baris caption "Gambar N." - sistem menambahkannya otomatis di bawah tiap gambar. Di dalam JSON, pemisah baris WAJIB ditulis \\n agar JSON tetap valid. Contoh sematan yang benar: {{"pengetahuan_content": "...urutan pelaksanaan: \\n```mermaid\\nflowchart LR\\nA[Mulai] --> B[Merencanakan pekerjaan]\\n```\\n...lanjutan materi..."}}
8. Key "evaluasi_pengetahuan" berisi DAFTAR bernomor "1) Pengetahuan tentang <butir pengetahuan silabus>" - satu butir per nomor, urut per elemen. Key "evaluasi_praktik" berisi daftar bernomor "1) <keterampilan>" urut per elemen. (Contoh catatan instruksi merah pada template sudah ada - Anda hanya mengisi daftarnya.) Key "lik_langkah_kerja" berisi langkah kerja praktik bernomor ("1. ...\\n2. ...") yang selaras dengan elemen/KUK silabus; "lik_peralatan" berisi daftar peralatan praktik (satu per baris); "lik_skenario" berisi skenario pekerjaan nyata yang menuntut peserta melaksanakan seluruh elemen kompetensi.
9. Gunakan tata bahasa Indonesia formal pemerintah yang sederhana dan mudah dipahami.
10. PASANGAN VERBA (WAJIB): setiap verba pasif pada indikator silabus WAJIB muncul dalam bentuk AKTIF berakar sama pada kolom "pengetahuan" dan "keterampilan" baris yang sama (indikator "Teridentifikasinya dasar, tujuan..." -> "mengidentifikasi maksud dan tujuan..."; "dicatat" -> "mencatat"). DILARANG memakai kata pasif "ter-" di pengetahuan/keterampilan; verba pasif hanya untuk indikator.
11. QUERY GAMBAR (WAJIB, ronde 15): sistem mengisi dokumen dengan GAMBAR (foto internet atau gambar hasil AI via Replicate), dan Anda yang menentukan query beserta modenya. RELEVANSI KETAT: setiap gambar wajib menggambarkan ISI SUBBAB itu sendiri - bila query tidak menggambarkan aktivitas konkret subbab, gambar akan ditolak sistem:
    - "cover_image_query": untuk COVER PAGE - STRING atau object {{"query": "...", "mode": "..."}} - scene/objek FISIK yang paling mewakili pekerjaan inti modul ini. Bahasa Inggris, deskripsi scene konkret (mis. modul penerimaan sampah -> "garbage truck weighing on weighbridge scale at landfill").
    - "image_queries" (LIST OF OBJECTS): SATU object PER subbab elemen, key: "subbab" (nomor elemen, mis. "1"), "query" (deskripsi scene bahasa Inggris 6-10 kata yang menggambarkan aktivitas SPESIFIK subbab itu - subjek fisik + aksi + tempat), "judul" (caption bahasa Indonesia yang menyebut aktivitas subbab), "mode" ("cari" atau "generate"). Jumlah objek = jumlah subbab elemen.
    - ATURAN QUERY: HARUS menyebut objek/benda FISIK yang terlibat di subbab (alat, kendaraan, lokasi, orang yang bekerja). Query abstrak tanpa objek fisik (mis. hanya "document checking", "report data", "administration") DILARANG - menghasilkan gambar yang tidak nyambung. Contoh BENAR utk subbab "Memeriksa dokumen pengiriman sampah": "worker checking waste manifest clipboard beside garbage truck". Contoh SALAH: "checking documents at office".
    - MODE: "cari" = FOTO nyata dari internet - pakai untuk scene berobjek fisik jelas (alat, mesin, kendaraan, lokasi kerja, aktivitas lapangan). "generate" = ilustrasi AI bergaya flat vector profesional - pakai untuk (a) proses abstrak/aliran data yang tak lazim difoto, (b) aktivitas administrasi/kantor yang fotonya jarang cocok (mengisi formulir, mencatat laporan) - untuk generate tulis DESKRIPSI ilustrasi lengkap dgn subjek fisiknya, mis. "worker writing waste reception report at desk with clipboard, computer and stacked documents".
    Query harus SPESIFIK ke alat/proses nyata pada subbab (bukan judul modul yang diulang-ulang)."""


def _close_truncated_json(text: str) -> str:
    """Tutup JSON yang terpotong karena budget num_predict habis (ronde 11b).

    Pindai karakter sambil melacak konteks (string/escape/stack bracket).
    Posisi AMAN = tepat setelah sebuah nilai selesai (string value, tutup
    bracket, literal). Bila output berhenti DI TENGAH string value, seluruh
    isi hingga EOF diselamatkan (kutip ditutup) - ini kasus paling sering:
    "pengetahuan_content" adalah string raksasa yang kepangkas. String key
    yang terpotong dibuang (kembali ke snapshot sebelumnya). Potongan lalu
    ditutup dengan bracket penutup sesuai stack, dan DIVALIDASI pemanggil.
    """
    stack = []  # "o" (object) | "a" (array)
    expect_key = False  # string BERIKUTNYA di object = key, bukan value
    in_string = False
    escape = False
    string_is_key = False  # string yang sedang terbuka dibuka saat menunggu key
    snapshot = None  # posisi aman terakhir (setelah nilai selesai)
    stack_at = []  # salinan stack saat snapshot
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
                # key atau value? lihat karakter non-spasi berikutnya
                j = i + 1
                while j < n and text[j] in " \t\r\n":
                    j += 1
                is_key = (j < n and text[j] == ":") or string_is_key
                if is_key:
                    expect_key = False  # setelah key: tunggu ':' (tak snapshot)
                else:
                    snapshot = i + 1
                    stack_at = stack[:]
            i += 1
            continue
        if ch == '"':
            in_string = True
            string_is_key = expect_key
        elif ch == "{":
            stack.append("o")
            expect_key = True
        elif ch == "[":
            stack.append("a")
            expect_key = False
        elif ch in "}]":
            if stack:
                stack.pop()
            expect_key = bool(stack and stack[-1] == "o")
            snapshot = i + 1
            stack_at = stack[:]
        elif ch == ":":
            expect_key = False
        elif ch == ",":
            expect_key = bool(stack and stack[-1] == "o")
        elif ch in "tfn-0123456789":
            # literal true/false/null/angka: potong di pembatas berikutnya
            j = i
            while j < n and text[j] not in ",}[] \t\r\n":
                j += 1
            if j < n:
                snapshot = j
                stack_at = stack[:]
            i = j
            continue
        i += 1

    if in_string:
        # output berhenti di tengah string
        if string_is_key:
            # key terpotong: buang, kembali ke nilai utuh terakhir
            if snapshot is None:
                raise ValueError("Tidak ada titik aman untuk menutup JSON terpotong.")
            tail, closer_stack = text[:snapshot], stack_at
        else:
            # string VALUE terpotong: seluruh isi diselamatkan + tutup kutip
            tail, closer_stack = text.rstrip("\\"), stack
            tail += '"'
            return tail + "".join("}" if c == "o" else "]" for c in reversed(closer_stack))
    else:
        if snapshot is None:
            raise ValueError("Tidak ada titik aman untuk menutup JSON terpotong.")
        tail, closer_stack = text[:snapshot], stack_at
    return tail + "".join("}" if c == "o" else "]" for c in reversed(closer_stack))


def _extract_json_object(text: str) -> dict:
    """Ambil objek JSON pertama dari output LLM (tahan markdown fence).

    Ronde 11b: tiga lapis - (1) parse baku, (2) strict=False (LLM kadang
    menulis newline/tab mentah di dalam string), (3) perbaikan JSON
    terpotong (budget num_predict habis) supaya draf yang 95% utuh tidak
    membuang seluruh siklus revisi.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    if start == -1:
        raise ValueError(f"Output LLM tidak berisi JSON: {text[:200]}...")
    end = text.rfind("}")
    # JSON terpotong di tengah string tak punya penutup '}' sama sekali -
    # raw = seluruh ekor teks sejak '{', lalu jalur perbaikan yang menutup.
    raw = text[start : end + 1] if end > start else text[start:]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        # strict=False: newline/tab mentah di dalam string tetap diterima
        return json.loads(raw, strict=False)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_close_truncated_json(raw), strict=False)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"JSON draf tidak valid: {exc}: {text[:200]}...")


# ======================================================================
# POST-PROCESSING DETERMINISTIK (ronde 12 - 16 komentar reviewer Word):
# hal yang berlaku audit KEMNAKER tidak diserahkan ke LLM saja.
# ======================================================================

# Baris naratif pembuka yang DILARANG di sel pengetahuan / deskripsi
# (komentar K5/K7: "terlalu banyak kata2", "ga perlu ada ini").
_NARRATIVE_RE = re.compile(
    r"^(?:peserta (?:mampu|dapat|harus|akan)|siswa (?:mampu|dapat)"
    r"|materi (?:mencakup|pelatihan)|materi pembelajaran mencakup)", re.I
)
_HEADING_LINE_RE = re.compile(r"^\s*(\d+)(?:\.(\d+))?\.?\s+(.*\S)\s*$")


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower()).strip(" .;,")


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm_text(a), _norm_text(b)).ratio()


def _cap(text: str) -> str:
    return (text[:1].upper() + text[1:]) if text else text


def _lower_first(text: str) -> str:
    return (text[:1].lower() + text[1:]) if text else text


def _clean_pengetahuan_cell(text: str) -> str:
    """Buang kalimat pembuka naratif ("Peserta mampu...") SEBELUM daftar
    bernomor; baris prefix resmi berakhiran ':' dipertahankan."""
    lines = [ln.strip() for ln in (text or "").split("\n") if ln.strip()]
    while (len(lines) > 1 and not _LIST_ITEM_RE.match(lines[0])
           and not lines[0].endswith(":")):
        lines.pop(0)
    return "\n".join(lines)


def _dedupe_numbered(text: str, seen: set) -> str:
    """Buang butir yang berulang antar baris elemen (komentar K5), lalu
    renumber "1. ", "2. ", ... Prefix berakhiran ':' dipertahankan."""
    lines = [ln.strip() for ln in (text or "").split("\n") if ln.strip()]
    prefix = ""
    if lines and not _LIST_ITEM_RE.match(lines[0]) and lines[0].endswith(":"):
        prefix = lines.pop(0)
    out = []
    for ln in lines:
        m = _LIST_ITEM_RE.match(ln)
        body = m.group(2).strip() if m else ln
        key = re.sub(r"\s+", " ", body.lower()).strip(" .;")
        if key in seen:
            continue
        seen.add(key)
        out.append(body)
    numbered = [f"{i}. {b}" for i, b in enumerate(out, start=1)]
    return "\n".join([prefix] + numbered) if prefix else "\n".join(numbered)


def _walk_subbab_titles(content: str, syllabus_rows: List[dict], fix: bool):
    """Pemindai bersama untuk perapian (fix=True) dan pemeriksaan evaluator
    (fix=False) judul subbab (komentar K10/K11).

    - Baris "N. <judul>"  -> canonical "N. <redaksi elemen N>".
    - Baris "N.M <judul>" -> canonical "N.M <redaksi butir pengetahuan M elemen N>".
    Snap hanya bila judul saat ini MIRIP (difflib >= 0.5) dengan redaksi -
    baris list bernomor di tengah paragraf (tidak mirip) tidak disentuh,
    dan nomor subbab wajib monotonik naik.
    Return (content_baru, daftar_pelanggaran) - pelanggaran hanya diisi
    ketika fix=False (mode evaluator).
    """
    expected = {}
    for r in syllabus_rows or []:
        if not isinstance(r, dict):
            continue
        try:
            n = int(str(r.get("elemen_no", "")).strip().rstrip("."))
        except ValueError:
            continue
        title = _cap(str(r.get("elemen", "")).strip().lstrip(". "))
        expected.setdefault(n, {"title": title,
                                "items": _pengetahuan_items(str(r.get("pengetahuan") or ""))})
    if not expected:
        return content, []

    violations: List[str] = []
    out = []
    in_mermaid = False
    cur_sub = 0
    cur_item = 0
    for line in content.split("\n"):
        st = line.strip()
        if st.startswith("```"):
            in_mermaid = not in_mermaid
            out.append(line)
            continue
        if in_mermaid:
            out.append(line)
            continue
        m = _HEADING_LINE_RE.match(st)
        if m:
            n = int(m.group(1))
            sub = int(m.group(2)) if m.group(2) else 0
            exp = expected.get(n)
            if sub:  # "N.M judul" sub-subbab
                if exp and n == cur_sub and cur_item < sub <= len(exp["items"]) \
                        and _similar(m.group(3), exp["items"][sub - 1]) >= 0.5:
                    canonical = f"{n}.{sub} {_cap(exp['items'][sub - 1])}"
                    if _norm_text(st) != _norm_text(canonical):
                        if fix:
                            line = canonical
                        else:
                            violations.append(
                                f'Judul sub-subbab "{st}" WAJIB persis redaksi '
                                f'pengetahuan silabus: "{canonical}"'
                            )
                    cur_item = sub
                    out.append(line)
                    continue
            else:  # "N. judul" subbab elemen
                if exp and (n == cur_sub + 1 or (cur_sub == 0 and n == 1)) \
                        and _similar(m.group(3), exp["title"]) >= 0.5:
                    canonical = f"{n}. {exp['title']}"
                    if _norm_text(st) != _norm_text(canonical):
                        if fix:
                            line = canonical
                        else:
                            violations.append(
                                f'Judul subbab "{st}" WAJIB persis redaksi elemen '
                                f'silabus: "{canonical}"'
                            )
                    cur_sub = n
                    cur_item = 0
                    out.append(line)
                    continue
        out.append(line)
    return "\n".join(out), violations


def _enforce_subbab_titles(content: str, syllabus_rows: List[dict]) -> str:
    return _walk_subbab_titles(content, syllabus_rows, fix=True)[0]


def _subbab_title_violations(content: str, syllabus_rows: List[dict]) -> List[str]:
    """Cek evaluator: judul subbab yang mirip tapi tidak persis redaksi
    silabus = pelanggaran (komentar K10/K11)."""
    return _walk_subbab_titles(content, syllabus_rows, fix=False)[1]


def _fix_deskripsi_unit(draft_json: dict, module: ModuleState) -> None:
    """Deskripsi unit WAJIB pendek 1 kalimat pola "Terimplementasinya ..."
    (komentar K7: contoh resmi ±130 karakter; kalimat "Peserta mampu..."/
    "Materi mencakup..." dilarang). Bila draf LLM melanggar -> timpa."""
    desc = str(draft_json.get("deskripsi_unit") or "").strip()
    unit = str(module.get("module_title") or "").strip()
    bad = (not desc or len(desc) > 350
           or _NARRATIVE_RE.search(desc)
           or "Terimplementasinya" not in desc)
    if bad:
        draft_json["deskripsi_unit"] = (
            f"Terimplementasinya {unit} sesuai prosedur kerja, spesifikasi "
            "teknis, dan standar keselamatan kerja yang berlaku."
        )


def _build_evaluasi_lists(draft_json: dict, syllabus_rows: List[dict]) -> None:
    """evaluasi_pengetahuan & evaluasi_praktik dibangun DETERMINISTIK dari
    silabus (komentar K18: ikuti catatan instruksi merah template - daftar
    "1) Pengetahuan tentang ..." urut per elemen)."""
    pen_lines = []
    prak_lines = []
    for r in syllabus_rows or []:
        if not isinstance(r, dict):
            continue
        for item in _pengetahuan_items(str(r.get("pengetahuan") or "")):
            pen_lines.append(
                f"{len(pen_lines) + 1}) Pengetahuan tentang {_lower_first(item.rstrip('.'))}"
            )
        ket = str(r.get("keterampilan") or "").strip()
        if ket:
            prak_lines.append(f"{len(prak_lines) + 1}) {_cap(ket.rstrip('.'))}")
    if pen_lines:
        draft_json["evaluasi_pengetahuan"] = "\n".join(pen_lines)
    if prak_lines:
        draft_json["evaluasi_praktik"] = "\n".join(prak_lines)


def agent2_node(module: ModuleState) -> dict:
    """Worker node: 1 silabus -> draft modul (draft_json).

    Input: ModuleState (payload dari Send API - bukan GlobalState).
    Output: partial update GlobalState -> {"modules": [modul terbaru]}.
    """
    tags: List[str] = get_template_tags()
    if not tags:
        raise RuntimeError("Template docx tidak punya tag {{ }} apa pun - periksa database/template_kemnaker.docx")

    feedback = (module.get("evaluator_feedback") or "").strip()
    previous_draft = module.get("draft_json") or {}
    if feedback and previous_draft:
        # Revisi: berikan draf sebelumnya agar LLM MEMPERBAIKI (bukan menulis ulang
        # dari nol) - sesuai PRD 4B "perbaiki draf sebelumnya".
        # Ronde 10: cap 15000 memotong ekor draf besar (pengetahuan_content bisa
        # 20 ribu+ char) - Agent 2 tidak melihat bagian yang dikritik evaluator
        # dan revisi justru MEMBUANG konten itu (loop FAIL berulang). Naik ke
        # 40000 char; budget output dijaga auto-bump invoke_with_retry.
        feedback_section = (
            f"=== FEEDBACK REVISI DARI EVALUATOR (WAJIB diperbaiki pada draf) ===\n{feedback}\n\n"
            f"=== DRAF SEBELUMNYA (perbaiki HANYA bagian yang dikritik di atas, pertahankan SEMUA bagian lain persis seperti semula) ===\n"
            f"{json.dumps(previous_draft, ensure_ascii=False, default=str)[:40000]}"
        )
    elif feedback:
        feedback_section = (
            f"=== FEEDBACK REVISI DARI EVALUATOR (WAJIB diperbaiki pada draf) ===\n{feedback}"
        )
    else:
        feedback_section = "Tidak ada feedback revisi - buat draf pertama."

    # Ronde 12 (K20): data penyusun ditanya Agent 1 DI AWAL percakapan,
    # dibawa ModuleState dan dipaksa ke draft (tidak diserahkan ke LLM).
    penyusun = module.get("penyusun") or {}
    prompt = SYSTEM_PROMPT.format(
        module_title=module["module_title"],
        kode_unit=module.get("kode_unit") or "-",
        alokasi_waktu=module.get("alokasi_waktu") or "-",
        nama_penyusun=str(penyusun.get("nama") or "-").strip() or "-",
        profesi_penyusun=str(penyusun.get("profesi") or "-").strip() or "-",
        syllabus=module["syllabus_content"],
        tags="\n".join(
            f'- "{t}"'
            for t in (
                list(tags)
                + [k for k in ROW_KEYS if k not in tags]
                + [k for k in IMAGE_QUERY_KEYS if k not in tags]
            )
        ),
        example=(get_example_module_text() or "(tidak tersedia)")[:12000],
        feedback_section=feedback_section,
        participants=PARTICIPANT_BRIEF,
    )

    # Mode AUTO (ronde 3): writer selalu effort "extra" - num_predict besar:
    # glm-5.3-flash:cloud adalah reasoning model - fase thinking menghabiskan
    # budget output TERLEBIH DAHULU (terukur: prompt 15k chars butuh >12000
    # token thinking saja) sebelum jawaban tercetak.
    llm = get_llm_for_effort(auto_effort("agent2"))

    last_err = None
    draft_json = {}
    for attempt in range(2):
        response = invoke_with_retry(llm, prompt, label="agent2")
        try:
            draft_json = _extract_json_object(response.content)
            # Validasi: minimal setengah tag harus terisi agar draf layak dievaluasi
            filled = [k for k, v in draft_json.items() if str(v).strip()]
            if not filled:
                raise ValueError("Semua nilai draft kosong.")
            # Ronde 10: cek blok mermaid DI SINI (aturan wajib #7) - kalau
            # hilang, minta agent2 melengkapi langsung (percobaan pertama)
            # daripada membiarkan evaluator mem-bounce satu putaran penuh.
            # Percobaan kedua tetap diterima tanpa mermaid -> evaluator +
            # loop revisi yang menangani (bukan error fatal).
            if attempt == 0 and not any("```mermaid" in str(v) for v in draft_json.values()):
                raise ValueError("Draf belum memuat blok ```mermaid di dalam nilai key pengetahuan_content.")
            # Validasi key: key tak dikenal dilaporkan keras (maksudnya salah ketik
            # LLM) lalu dibuang agar tidak bocor ke context docxtpl.
            # Row-loop docxtpl ({%tr %}) tidak terdeteksi regex tag sederhana,
            # jadi masuk whitelist eksplisit (ronde 11: bahan/cek/kamus/referensi).
            known_keys = set(tags) | set(ROW_KEYS) | set(IMAGE_QUERY_KEYS)
            unknown = set(draft_json) - known_keys
            if unknown:
                print(f"[agent2] Key draft tidak dikenal (dibuang): {sorted(unknown)}")
                for k in unknown:
                    draft_json.pop(k, None)
            # Ronde 6: LLM kadang lupa menyertakan "elemen_rows" (terukur
            # 2026-09-07 - tabel elemen di docx final kosong tanpa peringatan).
            # Fallback deterministik: bangun dari syllabus_rows state.
            if not draft_json.get("elemen_rows"):
                fb_rows = [
                    {
                        # Ronde 11: elemen_no/kuk_no/durasi wajib ada - kolom
                        # silabus 9 kolom template resmi 2024 butuh semuanya.
                        "elemen_no": r.get("elemen_no", "") or "",
                        "elemen": f"{r.get('elemen_no', '')} {r.get('elemen', '')}".strip(),
                        "kuk_no": r.get("kuk_no", "") or "",
                        "kuk": f"{r.get('kuk_no', '')} {r.get('kuk', '')}".strip(),
                        "durasi": r.get("durasi", "") or "",
                        # Ronde 8: indikator ikut dibawa agar penyelarasan
                        # pasangan verba di bawah tetap bekerja pada baris
                        # fallback (keterampilan = pasangan aktif indikator).
                        "indikator": r.get("indikator", "") or "",
                        "pengetahuan": r.get("pengetahuan", "") or "",
                        "keterampilan": r.get("keterampilan", "") or "",
                    }
                    for r in (module.get("syllabus_rows") or [])
                ]
                if fb_rows:
                    print("[agent2] elemen_rows kosong dari LLM - dibangun dari syllabus_rows")
                    draft_json["elemen_rows"] = fb_rows
            # Kolom pengetahuan pada tabel elemen dipaksa bernomor urut
            # "1. ", "2. ", ... (ronde 2) + pasangan verba indikator(pasif)
            # -> pengetahuan/keterampilan(aktif) di-enforce DI SINI, setelah
            # fallback, agar SEMUA baris (LLM maupun fallback) lolos.
            # Ronde 12 (K5): kalimat naratif dibuang + butir duplikat lintas
            # baris elemen dihapus sebelum penomoran urut "1. ", "2. ", ...
            seen_items: set = set()
            for row in draft_json.get("elemen_rows") or []:
                if isinstance(row, dict):
                    row["pengetahuan"] = _dedupe_numbered(
                        number_pengetahuan(
                            _clean_pengetahuan_cell(str(row.get("pengetahuan") or "")),
                            str(row.get("indikator") or ""),
                        ),
                        seen_items,
                    )
                    row["keterampilan"] = to_active_voice(
                        str(row.get("keterampilan") or ""), str(row.get("indikator") or "")
                    )
            # Ronde 12 (K10/K11): judul subbab PERSIS redaksi silabus.
            draft_json["pengetahuan_content"] = _enforce_subbab_titles(
                str(draft_json.get("pengetahuan_content") or ""),
                module.get("syllabus_rows") or [],
            )
            # Ronde 12 (K7): deskripsi unit pendek pola "Terimplementasinya ...".
            _fix_deskripsi_unit(draft_json, module)
            # Ronde 12 (K18): evaluasi pengetahuan/praktik = daftar urut silabus.
            _build_evaluasi_lists(draft_json, module.get("syllabus_rows") or [])
            # Ronde 12 (K20): penyusun dari state - tidak diserahkan ke LLM.
            draft_json["nama_penyusun"] = str(penyusun.get("nama") or "").strip() or "-"
            draft_json["profesi_penyusun"] = str(penyusun.get("profesi") or "").strip() or "-"
            draft_json["nama_penyusun_2"] = str(penyusun.get("nama2") or "").strip() or "-"
            draft_json["profesi_penyusun_2"] = str(penyusun.get("profesi2") or "").strip() or "-"
            # Ronde 13: query gambar WAJIB ada (foto internet di cover +
            # tiap subbab). LLM yang lupa -> fallback deterministik.
            if not str(draft_json.get("cover_image_query") or "").strip():
                draft_json["cover_image_query"] = module["module_title"]
            iq = draft_json.get("image_queries")
            # Ronde 16 ketersediaan: JAMININ setiap subbab elemen punya entri
            # image_queries - gabungkan entri yang DIBERIKAN LLM dengan fallback
            # utk subbab yang masih kosong (sebelumnya fallback hanya jika
            # image_queries kosong total; bila LLM mengisi sebagian, sisanya
            # kosong dan subbab itu kehilangan gambar).
            llm_map: dict = {}
            if isinstance(iq, list):
                for e in iq:
                    if isinstance(e, dict) and str(e.get("subbab") or "").strip():
                        llm_map[str(e.get("subbab")).strip().rstrip(".")] = e
            seen_elem: set = set()
            iq_merged = []
            for r in draft_json.get("elemen_rows") or []:
                if not isinstance(r, dict):
                    continue
                no = str(r.get("elemen_no", "")).strip().rstrip(".")
                if not no or no in seen_elem:
                    continue
                seen_elem.add(no)
                if no in llm_map:
                    iq_merged.append(llm_map[no])
                else:
                    elemen_txt = re.sub(
                        r"^\d+\.\s*", "", str(r.get("elemen") or "")).strip()
                    iq_merged.append({
                        "subbab": no,
                        "query": f"{module['module_title']} {elemen_txt}".strip(),
                        "judul": elemen_txt or module["module_title"],
                    })
            if iq_merged:
                draft_json["image_queries"] = iq_merged
            break
        except ValueError as exc:
            last_err = exc
            prompt = (
                f"{prompt}\n\n"
                f"PERINGATAN: jawaban Anda sebelumnya tidak valid ({exc}). "
                f"Jawab ULANG dengan satu objek JSON valid saja."
            )
    else:
        raise RuntimeError(f"Agent 2 gagal menghasilkan draft JSON: {last_err}")

    updated = {
        **module,
        "draft_json": draft_json,
    }
    # Kembalikan sebagai partial update GlobalState (reducer merge_modules
    # akan upsert modul ini by module_id ke daftar global).
    return {"modules": [updated]}