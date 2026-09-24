"""
DOC UTILS - Ekstraksi teks .docx & penemuan tag template
=========================================================
- extract_docx_text(): teks program pelatihan / contoh modul (untuk konteks
  prompt Agent 1 & Agent 2) — memakai python-docx (dependency docxtpl).
- get_template_tags(): temukan semua placeholder {{ nama_tag }} di dalam
  template_kemnaker.docx, sehingga Agent 2 tahu PERSIS key apa saja yang
  harus diisi draft_json-nya (tanpa hardcode).
"""

import os
import re
import zipfile
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Union

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

TEMPLATE_PATH = _PROJECT_ROOT / os.getenv(
    "TEMPLATE_PATH", "database/template_kemnaker.docx"
)
PROGRAM_DOC_PATH = os.getenv("PROGRAM_DOC_PATH", "")
EXAMPLE_MODULES_DIR = os.getenv("EXAMPLE_MODULES_DIR", "")
OUTPUT_DIR = _PROJECT_ROOT / os.getenv("OUTPUT_DIR", "output")

# ----------------------------------------------------------------------
# Program aktif (feedback ronde 2 item 4): user bisa upload program docx
# miliknya (final atau draft) - semua ekstraksi mengikuti program aktif.
# Default = PROGRAM_DOC_PATH dari .env.
#
# Deployment multi-user: global _ACTIVE_PROGRAM adalah DEFAULT ORGANISASI
# (dari upload terakhir siapa pun). Untuk sesi per-user, server mencap
# program ke ContextVar di thread pemanggil SEBELUM graph.invoke - fan-out
# Agent2/3 mewarisi context ini (threading.local tidak terbawa ke thread
# pool LangGraph, contextvars terbawa). Prioritas: ContextVar -> global
# -> default .env.
# ----------------------------------------------------------------------
_ACTIVE_PROGRAM: Optional[str] = None
_ACTIVE_PROGRAM_CV: ContextVar[Optional[str]] = ContextVar("active_program", default=None)


def set_active_program(path: Union[str, Path]) -> str:
    """Set program docx aktif (hasil upload user). Return path resolved."""
    global _ACTIVE_PROGRAM
    _ACTIVE_PROGRAM = str(Path(path).resolve())
    return _ACTIVE_PROGRAM


def set_context_program(path: Optional[Union[str, Path]]) -> "object":
    """Cap program aktif untuk CONTEXT SAAT INI (per-sesi, aman fan-out).
    Return token ContextVar - simpan lalu kembalikan via ContextVar.reset."""
    if path:
        return _ACTIVE_PROGRAM_CV.set(str(Path(path).resolve()))
    return _ACTIVE_PROGRAM_CV.set(None)


def get_active_program() -> str:
    """Path program docx aktif: program sesi (ContextVar) bila diset,
    selain itu default organisasi/global atau default .env."""
    cv_val = _ACTIVE_PROGRAM_CV.get()
    if cv_val:
        return cv_val
    return _ACTIVE_PROGRAM or str(PROGRAM_DOC_PATH)

# Pola placeholder Jinja sederhana: {{ nama_tag }} dan varian paragraf
# {{p nama_tag }} (docxtpl Subdoc - pengetahuan_content berisi gambar/paragraf)
_TAG_RE = re.compile(r"\{\{p?\s*([a-zA-Z0-9_]+)\s*\}\}")


# Item list di sel Pengetahuan: "1. ...", "1) ...", "1: ..." (di awal baris,
# pemisah wajib agar "3 jam pelajaran" tidak salah terbaca sebagai nomor item)
_LIST_ITEM_RE = re.compile(r"^\s*(\d+)\s*[.)\]:-]\s+(.*)$")


# ----------------------------------------------------------------------
# Verba pasif -> aktif (feedback ronde 6): kolom Pengetahuan wajib kata
# kerja AKTIF (meN-/ber-), sedangkan indikator di dokumen program lazim
# pasif ("dicatat", "diterapkan"). Konversi mengikuti kaidah morfologi
# meN- baku Bahasa Indonesia.
_NASAL_KEEP_P = ("pr", "pl")  # mempraktikkan, memproses: p dipertahankan
# Pengecualian ejaan yang tidak mengikuti tabel kaidah (bentuk baku KBBI).
# Ronde 8b: pasif yang melepas akhiran -kan (indikator nyata "Terlaksananya
# ...") wajib kembali ke verba aktif -kan penuh: "terlaksananya" -> "melaksanakan",
# BUKAN "melaksana" (bentuk terpotong yang tidak baku).
_NASAL_OVERRIDE = {
    "nilai": "menilai",
    "laksana": "melaksanakan", "laksanakan": "melaksanakan",
    "wujud": "mewujudkan",
    "kendali": "mengendalikan",
    "kumpul": "mengumpulkan",
    "cantum": "mencantumkan",
    "lampir": "melampirkan",
    "sosialisasi": "mensosialisasikan",
    "dokumentasi": "mendokumentasikan",
    "koordinasi": "mengoordinasikan", "koordinasikan": "mengoordinasikan",
    "informasi": "menginformasikan",
    "komunikasi": "mengkomunikasikan",
    "implementasi": "mengimplementasikan",
    "integrasi": "mengintegrasikan",
    "interpretasi": "menginterpretasikan",
    "evaluasi": "mengevaluasi",
    "identifikasi": "mengidentifikasi",
    "analisis": "menganalisis",
    "simulasi": "mensimulasikan",
    "kalibrasi": "mengkalibrasi",
    "verifikasi": "memverifikasi",
}


def _nasal_active(root: str) -> str:
    """meN- + akar kata (kaidah baku KBBI: mem-, meny-, men-, meng-, me-)."""
    if root in _NASAL_OVERRIDE:
        return _NASAL_OVERRIDE[root]
    c = root[:1]
    if c in "pm":  # pilih->memilih, pinjam->meminjam, miliki->memiliki, minta->meminta
        return "mem" + (root if root[:2] in _NASAL_KEEP_P else root[1:])
    if c in "bfv":  # buat, fasilitasi, verifikasi
        return "mem" + root
    if c == "t":  # tulis -> menulis, terapkan -> menerapkan (t dihilangkan)
        return "men" + root[1:]
    if c in "cdjn":  # catat, dengar, jawab, nilai
        return "men" + root
    if c == "s":
        # simpan -> menyimpan; cluster s+konsonan (struktur) -> men + akar utuh
        if len(root) > 1 and root[1] not in "aeiou":
            return "men" + root
        return "meny" + root[1:]
    if c == "k":  # kaji -> mengaji, kerjakan -> mengerjakan (k dihilangkan)
        return "meng" + root[1:]
    if c in "lrwy":  # lakukan -> melakukan, rakit -> merakit, warnai -> mewarnai
        return "me" + root
    return "meng" + root  # vokal, g, h (hitung -> menghitung, ajar -> mengajar)


# Kata berawalan "di"/"ter" yang BUKAN verba pasif - jangan disentuh.
_PASSIVE_BLOCKLIST = frozenset({
    "dioda", "dimensi", "dinas", "diantara", "terdiri", "terjadwal", "secara",
    # nomina umum berawalan di-/ter- (ronde 8 - "Diameter" tadinya menghasilkan
    # pasangan hantu "mengameter" dan mengganti verba keterampilan)
    "diameter", "diagram", "dinamis", "dinamika", "diagnosis", "diet", "dilema",
    "terapi", "teror", "termostat", "terpal", "terigu", "terasi",
})


def passive_to_active(word: str) -> str:
    """'dicatat' -> 'mencatat', 'diterapkan' -> 'menerapkan', dst.

    Bentuk pasif bersonda "-nya" (contoh nyata program PLTSa: indikator
    "Teridentifikasinya dasar, tujuan, perintah kerja...") dikonversi ke
    verba aktif MURNI tanpa "-nya": "mengidentifikasi" - bentuk inilah yang
    dipakai kolom pengetahuan/keterampilan.

    Dikembalikan apa adanya bila pola pasif tidak cocok (jangan mengarang).
    """
    low = word.lower().strip()
    core = low[: -3] if low.endswith("nya") else low  # "terX-nya" -> "terX"
    if core.startswith("di") and len(core) > 4 and core[2].isalpha():
        root = core[2:]
    elif core.startswith("ter") and len(core) > 5 and core[3].isalpha():
        root = core[3:]
    else:
        return word
    if low in _PASSIVE_BLOCKLIST:
        return word
    active = _nasal_active(root)
    if word[:1].isupper():
        return active.capitalize()
    return active  # pasif nominal "terX-nya" -> verba aktif murni


def _active_first_word(item: str, indicator_words: frozenset) -> str:
    """Konversi kata PERTAMA item pengetahuan pasif -> aktif, HANYA bila verba
    itu memang disalin dari kolom indikator (anti salah potong: "dioda",
    "dimensi" dsb. tidak disentuh walau berawalan di-)."""
    m = re.match(r"\s*([A-Za-z\-]+)", item)
    if not m:
        return item
    word = m.group(1)
    low = word.lower()
    if low in indicator_words and low not in _PASSIVE_BLOCKLIST:
        active = passive_to_active(word)
        if active != word:
            return active + item[m.end(1):]
    return item


def to_active_voice(text: str, indikator: str = "") -> str:
    """Sel KETERAMPILAN: kata utama WAJIB pasangan aktif dari verba pasif
    kolom indikator baris yang sama (ronde 8, diperkuat dari ronde 6 - user:
    "harus disamakan kata utamanya, pasif di indikator -> aktif di
    keterampilan").

    Contoh nyata: indikator "Teridentifikasinya dasar, tujuan, perintah
    kerja, perlengkapan K2..." -> keterampilan "Mengidentifikasi maksud dan
    tujuan pengoperasian Kondenser...".

    Ronde 17: bila sel KETERAMPILAN kosong/tak berawal huruf (placeholder
    seperti "." atau spasi) tetapi indikator memuat verba pasif, ISI dengan
    pasangan AKTIF-nya (kandidat pertama) - jangan biarkan sel kosong
    padahal indikator punya verba yang bisa diaktifkan. Bila indikator tanpa
    verba pasif, kembalikan apa adanya (tidak mengarang).
    """
    text = (text or "").strip()
    candidates = _expected_actives(indikator)
    # Ronde 17: sel Keterampilan kosong/tak berawal huruf (placeholder "." dsb.)
    # tetapi indikator punya verba pasif -> isi pasangan AKTIF-nya.
    if not re.match(r"[A-Za-z]", text) and candidates:
        return cap_first(candidates[0])
    if not text:
        return text
    return _align_verb(text, candidates)


# Modal yang dibuang dari depan kalimat keterampilan (gaya Kemnaker: langsung
# verba kerja - "Dapat mengidentifikasi..." -> "Mengidentifikasi...").
_MODAL_WORDS = frozenset({"dapat", "mampu", "bisa"})


def _expected_actives(indikator: str) -> List[str]:
    """Daftar verba AKTIF pasangan bagi SEMUA verba pasif di kolom indikator
    (urut kemunculan). Kosong bila indikator tidak memuat verba pasif di-/
    ter- (jangan mengarang pasangan)."""
    out: List[str] = []
    for w in re.findall(r"[A-Za-z\-]+", indikator or ""):
        act = passive_to_active(w)
        if act.lower() != w.lower():
            a = act.lower()
            if a not in out:
                out.append(a)
    return out


def _looks_verb(word: str) -> bool:
    """Heuristik verba berawalan meN-/ber- (melakukan, mengoperasikan,
    merakit, berlatih) - cukup untuk kata PERTAMA sel keterampilan."""
    low = word.lower()
    return len(low) > 4 and low.startswith(
        ("mem", "men", "meng", "meny", "mel", "mer", "mew", "ber")
    )


def _with_case(new: str, old: str) -> str:
    # Hanya kapital di huruf pertama - str.capitalize() mengecilkan SEMUA
    # huruf lain ("Kondenser" -> "kondenser", bug yang terukur di test).
    return (new[:1].upper() + new[1:]) if old[:1].isupper() else new


def _align_verb(text: str, candidates: List[str]) -> str:
    """Samakan kata PERTAMA sel keterampilan dengan pasangan aktif verba
    pasif indikator. Prioritas:
    1. verba depan sudah salah satu pasangan -> biarkan (akar cocok, contoh
       indikator "Diameter benda diukur" + keterampilan "Mengukur..." tidak
       dirusak walau ada nomina berawalan di- di indikator);
    2. verba pasif di-/ter- yang AKARnya sama -> konversi morfologi meN-;
    3. verba lain (aktif meN-/ber- atau pasif berakar beda) -> DIGANTI dengan
       pasangan utama (kandidat pertama) indikator;
    4. modal "dapat/mampu/bisa" di depan -> dibuang lalu ulangi;
    5. mulai dengan nomina -> pasangan aktif disisipkan di depan.
    Bila indikator tanpa verba pasif (kandidat kosong), hanya verba pasif
    yang dikonversi - jangan mengarang pasangan."""
    m = re.match(r"\s*([A-Za-z\-]+)", text)
    if not m:
        return text
    word, rest = m.group(1), text[m.end(1):]
    low = word.lower()

    if low in _MODAL_WORDS:  # "Dapat mengidentifikasi..." -> "Mengidentifikasi..."
        inner = _align_verb(rest.lstrip(), candidates)
        return _with_case(inner, word) if inner else text

    act = passive_to_active(word)
    is_passive = act.lower() != low

    if candidates and low in candidates:
        return text
    if is_passive and (not candidates or act.lower() in candidates):
        return _with_case(act, word) + rest
    if candidates and (_looks_verb(word) or is_passive):
        return _with_case(candidates[0], word) + rest
    if candidates:  # mulai dengan nomina -> sisipkan verba pasangan di depan
        body = text[m.start(0):].lstrip()
        body = body[:1].lower() + body[1:]
        return _with_case(candidates[0], word) + " " + body
    if is_passive:
        return _with_case(act, word) + rest
    return text


def cap_first(text: str) -> str:
    """Huruf pertama besar SAJA (str.capitalize() mengecilkan sisanya)."""
    return (text[:1].upper() + text[1:]) if text else text


# Baris naratif pembuka yang DILARANG di sel pengetahuan (ronde 12, komentar
# reviewer K5/K7: "terlalu banyak kata2", "ga perlu ada ini").
_NARRATIVE_RE = re.compile(
    r"^(?:peserta (?:mampu|dapat|harus|akan)|siswa (?:mampu|dapat)"
    r"|materi (?:mencakup|pelatihan)|materi pembelajaran mencakup)", re.I
)


def pengetahuan_items(text: str) -> List[str]:
    """Butir pengetahuan dari sel silabus (baris prefix/naratif dibuang).

    Sel lazimnya berformat "Penjelasan tentang:\\n1. ...\\n2. ..." dari tabel
    acuan program; dipakai injector (judul sub-subbab legal) dan Agent 2
    (pembangunan daftar evaluasi)."""
    items: List[str] = []
    for ln in (text or "").split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        m = _LIST_ITEM_RE.match(ln)
        if m:
            items.append(m.group(2).strip())
        elif not items and not ln.endswith(":") and not _NARRATIVE_RE.match(ln):
            items.append(ln)  # sel tanpa penomoran: baris pertama = butir
    return items


def number_pengetahuan(text: str, indikator: str = "") -> str:
    """Normalisasi sel Pengetahuan -> penomoran urut "1. ", "2. ", ... (feedback
    bos ronde 2: kolom Pengetahuan WAJIB bernomor dan berurutan).

    - Prefix naratif (mis. "Penjelasan tentang:") dipertahankan apa adanya.
    - Ada penomoran di teks -> pecah per nomor (baris lanjutan menyatu ke item
      sebelumnya) dan RENUMBER sequential mengabaikan nomor asli yang
      bolong/tak urut.
    - Tanpa penomoran -> tiap baris (atau bagian dipisah ";") = satu item.
    - Teks kosong -> tetap kosong (jangan bikin "1. " hantu).
    - Ronde 6: verba pasif di-/ter- yang disalin dari kolom indikator
      dikonversi ke aktif (meN-) - kolom Pengetahuan wajib kata kerja aktif.
    """
    text = (text or "").strip()
    if not text:
        return ""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    prefix = ""
    if lines and not _LIST_ITEM_RE.match(lines[0]) and lines[0].endswith((':', '-')):
        prefix = lines.pop(0)

    items: List[str] = []
    if any(_LIST_ITEM_RE.match(ln) for ln in lines):
        current: List[str] = []
        for ln in lines:
            m = _LIST_ITEM_RE.match(ln)
            if m:
                if current:
                    items.append(" ".join(current))
                    current = []
                current.append(m.group(2).strip())
            elif current:
                current.append(ln)  # baris lanjutan item sebelumnya
            else:
                items.append(ln)
        if current:
            items.append(" ".join(current))
    elif len(lines) == 1 and ";" in lines[0]:
        items = [p.strip() for p in lines[0].split(";") if p.strip()]
    else:
        items = lines

    indicator_words = frozenset(re.findall(r"[a-z]+", (indikator or "").lower()))
    items = [_active_first_word(it, indicator_words) for it in items]

    numbered = [f"{i}. {it}" for i, it in enumerate(items, start=1)]
    out = "\n".join(numbered)
    return f"{prefix}\n{out}" if prefix else out


def get_template_tags(docx_path: Union[str, Path, None] = None) -> List[str]:
    """Kembalikan daftar nama tag unik dari template .docx (urut abjad).

    Hanya mengambil tag variabel sederhana ({{ tag }}) — blok kontrol
    ({% for %} / {% if %}) tidak termasuk karena diisi otomatis oleh
    docxtpl saat render.
    """
    path = Path(docx_path) if docx_path else TEMPLATE_PATH
    tags: set = set()
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            # document.xml + header/footer XML (tag bisa muncul di sana juga)
            if not (name.startswith("word/") and name.endswith(".xml")):
                continue
            xml = z.read(name).decode("utf-8", "replace")
            tags.update(_TAG_RE.findall(xml))
    return sorted(tags)


@lru_cache(maxsize=4)
def _extract_docx_cached(path_str: str) -> str:
    import docx  # python-docx, dependency dari docxtpl

    doc = docx.Document(path_str)
    parts: List[str] = []
    for para in doc.paragraphs:
        if para.text.strip():
            parts.append(para.text.strip())
    for table in doc.tables:  # teks dalam tabel (mis. tabel silabus)
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def extract_docx_text(path: Union[str, Path], max_chars: int = None) -> str:
    """Ekstrak teks (paragraf + tabel) dari file .docx. Kosongkan jika file tak ada."""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return ""
    text = _extract_docx_cached(str(p))
    if max_chars:
        text = text[:max_chars]
    return text


def get_program_text(max_chars: int = 6000) -> str:
    """Teks program pelatihan (konteks wajib Agent 1) - mengikuti program aktif."""
    return extract_docx_text(get_active_program(), max_chars)


@lru_cache(maxsize=1)
def get_example_module_text() -> str:
    """Cuplikan contoh modul pertama (acuan gaya penulisan Agent 2)."""
    if not EXAMPLE_MODULES_DIR:
        return ""
    files = sorted(Path(EXAMPLE_MODULES_DIR).glob("*.docx"))
    if not files:
        return ""
    return extract_docx_text(files[0], max_chars=12000)


def template_info() -> Dict:
    """Info template untuk debugging / tampilan UI."""
    tags = get_template_tags()
    return {"path": str(TEMPLATE_PATH), "exists": TEMPLATE_PATH.exists(), "tags": tags}