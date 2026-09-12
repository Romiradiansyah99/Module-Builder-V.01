"""
UNIT EXTRACTOR - Ekstraksi deterministik daftar unit kompetensi dari dokumen program
====================================================================================
Sumber kebenaran = tabel "Daftar Unit Kompetensi" + tabel identitas & silabus
per unit di PROGRAM_DOC_PATH (mis. Program_Pelatihan_PLTSa_Revisi_Kemenaker.docx).

Kenapa bukan LLM/RAG? Daftar unit adalah data terstruktur di docx - parsing
python-docx jauh lebih andal daripada meminta glm mengutip 27 baris tabel
(salah satu karakter pun membuat module_title tidak identik dengan sumber),
dan RAG chunk tidak reliably menampilkan tabel daftar unit.

Output utama:
- extract_program_units()      -> [{no, kelompok, judul, kode, teori, praktek, jumlah, alokasi, capaian}]
- extract_unit_syllabus_rows() -> {"1.1": [{elemen_no, elemen, kuk_no, kuk, indikator, pengetahuan, keterampilan, durasi}], ...}
- get_units_block()            -> blok teks untuk prompt Agent 1
- format_units_markdown()      -> daftar markdown untuk balasan chat
- match_unit(units, title)     -> snap judul LLM ke judul unit kanonik

Toleransi data (terverifikasi pada file program aktual):
- Header daftar unit punya 2 baris (row 0 merged stub, row 1 header asli).
- Unit 1.9 kosong waktu; 2.3 kode "-"; kelompok III tidak punya unit.
- Tabel silabus unit 1.4/1.5/1.8 kerangka saja (indikator/pengetahuan/keterampilan
  kosong) -> rows_status "partial", LLM mengisi bagian kosong by kuk_no.
- Unit 2.1-2.3 tidak punya tabel acuan sama sekali -> rows_status "none".
- Baris terakhir tabel silabus bisa "Asesmen" (1 sel) -> dilewati.
"""

import difflib
import re
from functools import lru_cache
from typing import Dict, List, Optional

from tools.doc_utils import get_active_program

# Baris silabus: 9 sel deduped (posisi tetap pada file aktual)
_ROW_KEYS = ("elemen_no", "elemen", "kuk_no", "kuk", "indikator",
             "pengetahuan", "keterampilan", "durasi")  # keterampilan_no dibuang

_ROW_KEYS_6COL = ("elemen", "kuk_no", "kuk", "indikator", "pengetahuan",
                  "keterampilan", "durasi")


def _program_path() -> str:
    """Program docx aktif (upload user via set_active_program, atau default .env)."""
    return get_active_program()


def _dedupe_cells(row) -> List[str]:
    """Sel merged vertikal muncul berulang di row.cells - dedupe by _tc identity."""
    out, seen = [], set()
    for c in row.cells:
        if id(c._tc) in seen:
            continue
        seen.add(id(c._tc))
        out.append(c.text.strip())
    return out


@lru_cache(maxsize=4)
def extract_program_meta(path: str = None) -> dict:
    """Judul program pelatihan (tabel 0, baris pertama: 'Judul | : | <nilai>')."""
    from docx import Document

    p = path or _program_path()
    try:
        doc = Document(p)
    except Exception:
        return {"judul": ""}
    for t in doc.tables[:2]:
        for r in t.rows:
            cells = _dedupe_cells(r)
            if len(cells) >= 4 and cells[1].lower().startswith("judul"):
                return {"judul": cells[-1].strip()}
    return {"judul": ""}


@lru_cache(maxsize=4)
def extract_program_units(path: str = None) -> tuple:
    """Daftar unit kompetensi dari tabel daftar unit.

    Returns tuple (bukan list) agar aman untuk lru_cache; pemanggil boleh
    list() jika perlu. Setiap item: {no, kelompok, judul, kode, teori,
    praktek, jumlah, alokasi, capaian}.
    """
    from docx import Document

    p = path or _program_path()
    try:
        doc = Document(p)
    except Exception:
        return ()

    units: List[dict] = []
    kelompok = ""
    no_re = re.compile(r"^(\d+\.\d+)\.?$")

    for t in doc.tables:
        if len(t.rows) < 3:
            continue
        header = " | ".join(_dedupe_cells(t.rows[0]) + _dedupe_cells(t.rows[1]))
        if "Judul Unit Kompetensi" not in header or "Kode Unit" not in header:
            continue
        for r in t.rows[2:]:
            c = _dedupe_cells(r)
            if not c:
                continue
            first = c[0].strip()
            m = no_re.match(first)
            if m:
                judul = c[1].strip() if len(c) > 1 else ""
                kode = c[2].strip() if len(c) > 2 else ""
                if not judul or judul.lower().startswith(("jumlah", "ket")):
                    continue
                teori = c[3].strip() if len(c) > 3 else ""
                praktek = c[4].strip() if len(c) > 4 else ""
                jumlah = c[5].strip() if len(c) > 5 else ""
                units.append({
                    "no": m.group(1), "kelompok": kelompok, "judul": judul,
                    "kode": "" if kode == "-" else kode,
                    "teori": teori, "praktek": praktek, "jumlah": jumlah,
                    "alokasi": "", "capaian": "",
                })
            elif re.match(r"^[IVX]+\.$", first) and len(c) > 1:
                kelompok = c[1].strip()  # "Kelompok Inti", dst.
        break  # hanya tabel daftar unit pertama

    # Lengkapi alokasi/capaian dari tabel identitas per unit
    meta = {u["no"]: u for u in units}
    for t in doc.tables:
        if len(t.rows) < 2:
            continue
        cells0 = _dedupe_cells(t.rows[0])
        if len(cells0) >= 4 and cells0[1].strip().lower() == "judul unit kompetensi":
            no = re.sub(r"\.$", "", cells0[0].strip())
            u = meta.get(no)
            if not u:
                continue
            if not u["judul"]:
                u["judul"] = cells0[-1].strip()
            for r in t.rows[1:]:
                c = _dedupe_cells(r)
                if len(c) >= 4:
                    label, val = c[1].strip().lower(), c[-1].strip()
                    if label.startswith("perkiraan waktu"):
                        u["alokasi"] = val
                    elif label == "kode" and not u["kode"]:
                        u["kode"] = "" if val == "-" else val
                    elif label == "capaian":
                        u["capaian"] = val
    return tuple(units)


@lru_cache(maxsize=4)
def extract_unit_syllabus_rows(path: str = None) -> Dict[str, tuple]:
    """Silabus terstruktur per unit: {no_unit: tuple(row_dict, ...)}.

    Pemetaan 9 sel (urutan terverifikasi): elemen_no, elemen, kuk_no, kuk,
    indikator, pengetahuan, keterampilan_no, keterampilan, durasi.
    Fallback 6+ sel: elemen, kuk_no, kuk, indikator, pengetahuan, keterampilan, durasi.
    """
    from docx import Document

    p = path or _program_path()
    try:
        doc = Document(p)
    except Exception:
        return {}

    result: Dict[str, List[dict]] = {}
    current_no = None
    for t in doc.tables:
        if len(t.rows) < 2:
            continue
        cells0 = _dedupe_cells(t.rows[0])
        flat0 = " / ".join(cells0).lower()
        if len(cells0) >= 4 and cells0[1].strip().lower() == "judul unit kompetensi":
            current_no = re.sub(r"\.$", "", cells0[0].strip())
            continue
        if "elemen" not in flat0 or "kriteria" not in flat0:
            continue  # bukan tabel silabus
        if not current_no:
            continue
        rows: List[dict] = []
        for r in t.rows[1:]:
            c = [x.strip() for x in _dedupe_cells(r)]
            # Baris "Asesmen" / baris kosong / header ulang -> skip
            if len(c) < 6 or c[0].lower() == "asesmen":
                continue
            if c[0].lower().startswith("elemen"):
                continue
            if len(c) >= 9:
                row = dict(zip(_ROW_KEYS, [c[0], c[1], c[2], c[3], c[4], c[5], c[7], c[8]]))
            else:
                row = dict(zip(_ROW_KEYS_6COL, c[:7]))
                row.setdefault("elemen_no", "")
            row = {k: (row.get(k) or "").strip() for k in _ROW_KEYS}
            # Durasi kotor ("JP" saja, "." saja) -> bersihkan
            if row["durasi"] in (".", "JP", ""):
                row["durasi"] = ""
            row["kuk_no"] = re.sub(r"\.$", "", row["kuk_no"])
            rows.append(row)
        if rows:
            result[current_no] = tuple(rows)
        current_no = None  # satu tabel silabus per unit

    # Status kelengkapan: complete / partial (kerangka saja) / none
    for no, rows in list(result.items()):
        filled = sum(1 for r in rows if r["pengetahuan"] and r["keterampilan"] and r["indikator"])
        if filled >= max(1, int(0.8 * len(rows))):
            continue  # complete
        # partial: buang sel kosong agar merge LLM by kuk_no jalan bersih
        cleaned = []
        for r in rows:
            cleaned.append({**r, "indikator": r["indikator"], "pengetahuan": r["pengetahuan"],
                            "keterampilan": r["keterampilan"]})
        result[no] = tuple(cleaned)
    return result


def rows_status(no: str, units=None) -> str:
    """'complete' | 'partial' | 'none' - penentu apakah LLM perlu mengisi rows."""
    rows = extract_unit_syllabus_rows().get(no)
    if not rows:
        return "none"
    # Ronde 5: WAJIB semua baris terisi penuh baru "complete" (dulu ambang 80% -
    # baris dengan indikator kosong lolos & tak pernah diisi walau user minta).
    filled = sum(1 for r in rows if r["pengetahuan"] and r["keterampilan"] and r["indikator"])
    return "complete" if filled == len(rows) else "partial"


def _format_rows_table(rows) -> str:
    lines = ["| elemen_no | elemen | kuk_no | kuk | indikator | pengetahuan | keterampilan | durasi |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| " + " | ".join(
            (r.get(k, "") or "").replace("\n", "; ").replace("|", "/") for k in _ROW_KEYS) + " |")
    return "\n".join(lines)


def get_units_block(units=None) -> str:
    """Blok DAFTAR UNIT untuk prompt Agent 1 - sumber kebenaran judul & kode."""
    units = list(units) if units is not None else list(extract_program_units())
    if not units:
        return ("(DAFTAR UNIT KOSONG - program kemungkinan masih draft. "
                "Usulkan daftar unit kompetensi dari isi program.)")
    lines = ["No | Judul Unit Kompetensi | Kode Unit | Jumlah JP | Ketersediaan tabel acuan"]
    for u in units:
        st = rows_status(u["no"])
        avail = {"complete": "lengkap", "partial": "kerangka (perlu dilengkapi)",
                 "none": "(belum tersedia - susun dari referensi SKKNI)"}[st]
        lines.append(f"| {u['no']} | {u['judul']} | {u['kode'] or '-'} | {u['jumlah'] or '-'} | {avail} |")
    return "\n".join(lines)


def format_units_markdown(units=None) -> str:
    """Daftar unit untuk balasan chat (markdown pipe table)."""
    units = list(units) if units is not None else list(extract_program_units())
    lines = ["| No | Judul Unit Kompetensi | Kode | Waktu |",
             "|---|---|---|---|"]
    for u in units:
        lines.append(f"| {u['no']} | {u['judul']} | {u['kode'] or '-'} | {u['jumlah'] or '-'} JP |")
    return "\n".join(lines)


def match_unit(units, title: str) -> Optional[dict]:
    """Snap judul LLM ke unit kanonik: exact -> casefold -> contains -> difflib."""
    t = (title or "").strip()
    if not t:
        return None
    for u in units:
        if u["judul"] == t:
            return u
    for u in units:
        if u["judul"].casefold() == t.casefold():
            return u
    tl = t.casefold()
    for u in units:
        if u["judul"].casefold() in tl or tl in u["judul"].casefold():
            return u
    close = difflib.get_close_matches(t, [u["judul"] for u in units], n=1, cutoff=0.6)
    if close:
        return next(u for u in units if u["judul"] == close[0])
    return None


if __name__ == "__main__":
    units = list(extract_program_units())
    print(f"{len(units)} unit:")
    for u in units:
        print(f"  {u['no']:6s} {u['judul'][:55]:57s} {u['kode']:18s} {u['jumlah']:>3s} JP [{rows_status(u['no'])}]")
    rows = extract_unit_syllabus_rows().get("1.1", ())
    print(f"\nSilabus 1.1: {len(rows)} baris; contoh: {rows[0] if rows else '-'}")