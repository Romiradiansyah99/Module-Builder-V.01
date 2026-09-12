"""
TEMPLATE BUILDER v2 - Bangun template tag {{ }} DARI TEMPLATE RESMI 2024
=======================================================================
Ronde 11 (feedback user: hasil .docx tidak sesuai template resmi - font,
spacing, cover, gambar hilang). Akar masalah versi lama: template dibangun
dari `Document()` kosong sehingga SAMPAUL, FONT (Bookman Old Style 12),
MARGIN, GAMBAR dan SECTION template resmi 2024 semuanya hilang.

Sekarang: ambil `database/template_kemnaker_original.docx` (salinan
byte-identik dari "01. Templet Modul_2024.docx") lalu sisipkan tag `{{ }}`
SECARA BEDAH di tempat yang tepat:
  - sampul: judul di textbox -> {{ judul_modul }} + kode {{ kode_unit }}
  - kata pengantar / pendahuluan: teks contoh -> tag
  - silabus: tabel identitas -> tag + tabel elemen/KUK/indikator (tbl2, 9 kolom)
    -> loop {%tr elemen_rows %}
  - pengetahuan: contoh isi dihapus -> {{ pengetahuan_content }}
  - LIK: skenario/gambar kerja/langkah/peralatan -> tag; tabel bahan (tbl4),
    cek observasi (tbl5), cek hasil (tbl6) -> loop {%tr bahan_rows %} dll.
  - lampiran: kamus (tbl7) -> {%tr kamus_rows %}, referensi (tbl8) ->
    {%tr referensi_rows %}, unit kompetensi (tbl9/tbl10) -> tag + loop
  - penyusun: tabel -> tag
Semua gaya (font, spacing, tabel, gambar, section) ikut dari template resmi.

Pelajaran bedah ronde 11:
  * JANGAN memakai indeks paragraf setelah ada penghapusan - selalu cari
    ulang dengan anchor teks.
  * Paragraf pembawa `w:sectPr` (pemisah section) JANGAN dihapus - cukup
    kosongkan isinya, kalau tidak jumlah section template berkurang.
  * Contoh isi di area LIK/lampiran berbentuk TABEL, bukan paragraf -
    diganti row-loop docxtpl agar tampilan tabel resmi tetap utuh.

Jalankan:
    python tools/template_builder.py
"""

import copy
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.doc_utils import TEMPLATE_PATH  # noqa: E402

SOURCE_PATH = TEMPLATE_PATH.with_name("template_kemnaker_original.docx")

# Ronde 12 (16 komentar reviewer Word): daftar isi STATIS (tabel contoh)
# diganti FIELD TOC Word asli (nomor & halaman di-update Word saat dokumen
# dibuka), judul bab diberi style Heading 1/2 agar masuk daftar isi, dan
# seluruh warna bukan merah dinormalisasi hitam (seragam ikut contoh modul).

# Judul bab level 1 (masuk daftar isi) - dicari via teks paragraf, bukan
# indeks, supaya tahan terhadap penghapusan contoh isi di tahap lain.
_H1_TEXTS = (
    "DAFTAR ISI", "KATA PENGANTAR", "PENDAHULUAN", "PANDUAN PENGGUNAAN MODUL",
    "SILABUS", "PENGETAHUAN", "Evaluasi Pengetahuan",
    "KETERAMPILAN DAN SIKAP KERJA", "Evaluasi Praktik", "EVALUASI PERSONAL",
    "Kamus Istilah", "Referensi", "Unit Kompetensi",
    "BATASAN VARIABEL", "PANDUAN PENILAIAN",
)
# Sub-bab level 2 (juga masuk daftar isi) - bagian dari bab Keterampilan.
_H2_PREFIXES = (
    "Lembar Cek Observasi", "Lembar Cek Hasil",
    "Lembar Instruksi Kerja (LIK)_2",
)
_H1_CONTAINS = ("PENYUSUN",)  # "Daftar Nama Penyusun" / "NAMA PENYUSUN"

# Biru aksen template (00B0F0) dihilangkan - reviewer: warna seragam hitam.
_BLUE = "00B0F0"


def _apply_heading_styles(doc) -> None:
    """Beri style Heading 1/2 pada paragraf judul bab (case-sensitif persis)
    agar field TOC menemukannya. Tampilan dipertahankan: run di-override
    Bookman Old Style 12 bold - style Heading hanya memberi outline level."""
    for para in doc.paragraphs:
        text = para.text.strip()
        if not text or len(text) > 100:  # judul LIK ber-tag cukup panjang
            continue
        target = None
        for pref in _H2_PREFIXES:  # cek H2 DULU (LIK_2 juga berawalan LIK)
            if text.startswith(pref):
                target = "Heading 2"
                break
        if target is None:
            if text in _H1_TEXTS or text.startswith("Lembar Instruksi Kerja (LIK)"):
                target = "Heading 1"
            elif _H1_CONTAINS and text.isupper() and any(
                    k in text.upper() for k in _H1_CONTAINS):
                target = "Heading 1"  # "NAMA PENYUSUN" - judul bab huruf besar
        if target is None:
            continue
        para.style = doc.styles[target]
        for r in para.runs:
            r.font.name = _BODY_FONT
            r.font.size = _BODY_SIZE
            r.bold = True


def _normalize_colors(doc) -> None:
    """Semua run berwarna biru (00B0F0) -> hitam; MERAH (FF0000, catatan
    instruksi) dipertahankan - sesuai komentar reviewer tentang seragam warna."""
    from docx.shared import RGBColor

    def fix_runs(runs):
        for r in runs:
            try:
                color = r.font.color
                if color is not None and color.type is not None \
                        and str(color.rgb).upper() == _BLUE:
                    color.rgb = RGBColor(0x00, 0x00, 0x00)
            except Exception:  # noqa: BLE001 - warna tak terbaca: lewati
                pass
    for para in doc.paragraphs:
        fix_runs(para.runs)
    for tbl in doc.tables:
        for row in tbl.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    fix_runs(para.runs)
    # textbox (judul sampul) - XML langsung
    for para in doc.paragraphs:
        for color_el in para._p.findall(".//w:txbxContent//w:color", _NS):
            if (color_el.get(qn("w:val")) or "").upper() == _BLUE:
                color_el.set(qn("w:val"), "000000")


def _replace_toc(doc) -> None:
    """Hapus tabel daftar isi statis (contoh entri + nomor halaman salah)
    dan pasang FIELD TOC Word asli: `TOC \\o "1-2" \\h \\z \\u`. Field ditandai
    dirty sehingga Word memperbarui nomor halaman saat dokumen dibuka."""
    toc_table = None
    for tbl in doc.tables:
        first = tbl.rows[0].cells[0].text.strip() if tbl.rows else ""
        if first.startswith("DAFTAR ISI") and "…" in first:
            toc_table = tbl
            break
    assert toc_table is not None, "Tabel daftar isi statis tidak ditemukan"
    anchor = doc.paragraphs[0]
    for p in doc.paragraphs:
        if p.text.strip() == "DAFTAR ISI":
            anchor = p
            break
    fld = (
        '<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:pPr><w:tabs><w:tab w:val="right" w:leader="dot" w:pos="9026"/></w:tabs></w:pPr>'
        '<w:r><w:fldChar w:fldCharType="begin" w:dirty="true"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> TOC \\o "1-2" \\h \\z \\u </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        '<w:r><w:rPr><w:rFonts w:ascii="Bookman Old Style" w:hAnsi="Bookman Old Style"/>'
        '<w:sz w:val="24"/></w:rPr><w:t>Daftar isi diperbarui otomatis oleh Word '
        '(bila tidak muncul: klik kanan > Update Field, atau Ctrl+A lalu F9).</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
    )
    from docx.oxml import parse_xml

    anchor._p.addnext(parse_xml(fld))
    # Tabel statis baru dihapus SETELAH field terpasang.
    tbl_el = toc_table._tbl
    tbl_el.getparent().remove(tbl_el)


def _enable_update_fields(doc) -> None:
    """settings.xml <w:updateFields w:val="true"/> -> Word otomatis memperbarui
    daftar isi saat dokumen dibuka. Elemen disisipkan SEBELUM footnotePr/
    endnotePr/compat agar urutan skema settings.xml tetap valid."""
    settings = doc.settings.element
    for existing in settings.findall(qn("w:updateFields")):
        existing.set(qn("w:val"), "true")
        return
    el = OxmlElement("w:updateFields")
    el.set(qn("w:val"), "true")
    before = None
    for tag in ("w:hdrShapeDefaults", "w:footnotePr", "w:endnotePr",
                "w:compat", "w:docVars", "w:rsids"):
        found = settings.find(qn(tag))
        if found is not None:
            before = found
            break
    if before is not None:
        before.addprevious(el)
    else:
        settings.append(el)

_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
}
_BODY_FONT = "Bookman Old Style"
_BODY_SIZE = Pt(12)


def _set_text(para, text: str, bold: bool = False) -> None:
    """Ganti seluruh isi paragraf dengan SATU run berformat body template."""
    # Buang semua run (pertahankan pPr/style paragraf)
    for r in list(para.runs):
        r._r.getparent().remove(r._r)
    run = para.add_run(text)
    run.font.name = _BODY_FONT
    run.font.size = _BODY_SIZE
    run.bold = bold


def _tag_para(doc_or_parent, tag: str, para=None, p_level: bool = False):
    """Jadikan `para` paragraf tag {{ tag }}; tanpa para -> paragraf baru.
    p_level=True -> varian paragraf {{p tag }} (docxtpl Subdoc: seluruh
    paragraf diganti konten subdoc - wajib untuk pengetahuan_content yang
    berisi gambar flowchart)."""
    if para is None:
        para = doc_or_parent.add_paragraph()
    _set_text(para, ("{{p " + tag + " }}") if p_level else ("{{ " + tag + " }}"))
    return para


def _carries_sectpr(para) -> bool:
    """True jika paragraf memuat w:sectPr (pemisah section) - JANGAN dihapus."""
    return bool(para._p.findall(".//w:sectPr", _NS))


def _clear_para(para) -> None:
    """Kosongkan isi paragraf TANPA menghapus elemennya (selamatkan sectPr)."""
    for r in list(para.runs):
        r._r.getparent().remove(r._r)


def _delete(paras) -> None:
    """Hapus kumpulan paragraf dari body. Paragraf pembawa sectPr hanya
    dikosongkan agar jumlah section template tetap utuh."""
    for p in paras:
        if _carries_sectpr(p):
            _clear_para(p)
        else:
            p._p.getparent().remove(p._p)


def _replace_sample(doc, start: int, end: int, tag: str):
    """Paragraf [start..end] (contoh isi template) -> satu paragraf tag.
    INDEKS MERUJUK TEMPLATE ASLI - hanya aman dipakai SEBELUM penghapusan."""
    paras = doc.paragraphs
    _tag_para(doc, tag, paras[start])
    _delete(paras[start + 1 : end + 1])


def _set_cell(cell, text: str) -> None:
    para = cell.paragraphs[0]
    _set_text(para, text)
    # hapus paragraf tambahan di sel yang sama
    for extra in cell.paragraphs[1:]:
        extra._p.getparent().remove(extra._p)


def _replace_textbox(para, markers: dict) -> bool:
    """Ganti isi textbox di dalam paragraf. `markers` = {marker: replacement};
    w:t yang memuat marker -> diganti replacement, SISA node teks pada textbox
    yang sama dikosongkan (judul sampul terpecah 3 run + kode unit di w:t
    terpisah)."""
    boxes = para._p.findall(".//w:txbxContent", _NS)
    hit = False
    for box in boxes:
        ts = box.findall(".//w:t", _NS)
        full = "".join(t.text or "" for t in ts)
        if not all(m in full for m in markers):
            continue
        hit = True
        for t in ts:
            txt = t.text or ""
            matched = None
            for m, rep in markers.items():
                if m in txt:
                    matched = rep
                    break
            t.text = matched if matched else ""
    return hit


def _cell_text_in_tr(tr_xml, idx: int, text: str) -> None:
    """Set teks sel ke-i pada XML <w:tr> yang sudah di-copy."""
    tcs = tr_xml.findall("w:tc", _NS)
    if idx >= len(tcs):
        return
    tc = tcs[idx]
    ps = tc.findall("w:p", _NS)
    for p in ps[1:]:
        tc.remove(p)
    first_p = ps[0]
    for r in first_p.findall("w:r", _NS):
        first_p.remove(r)
    r = first_p.makeelement("{%s}r" % _NS["w"], {})
    rpr = r.makeelement("{%s}rPr" % _NS["w"], {})
    rfonts = r.makeelement("{%s}rFonts" % _NS["w"], {})
    rfonts.set("{%s}ascii" % _NS["w"], _BODY_FONT)
    rfonts.set("{%s}hAnsi" % _NS["w"], _BODY_FONT)
    sz = r.makeelement("{%s}sz" % _NS["w"], {})
    sz.set("{%s}val" % _NS["w"], "24")
    rpr.append(rfonts)
    rpr.append(sz)
    r.append(rpr)
    t = r.makeelement("{%s}t" % _NS["w"], {})
    t.text = text
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    r.append(t)
    first_p.append(r)


def _make_table_loop(table, header_row_idx: int, data_row_idx: int,
                     loop_tag: str, body_cells: dict, keep_rows: int) -> None:
    """Sisipkan row-loop docxtpl ({%tr for r in <loop_tag> %}) ke tabel.

    header_row_idx : baris acuan penyisipan (loop mulai SETELAH baris ini)
    data_row_idx   : baris data asli yang di-copy untuk struktur loop
    body_cells     : {idx_sel: teks_tag} untuk baris body loop
    keep_rows      : jumlah baris awal yang DIPERTAHANKAN (header)
    """
    data_tr = table.rows[data_row_idx]._tr
    orig_rows = [row._tr for row in table.rows]
    keep_ids = {id(orig_rows[i]) for i in range(keep_rows)}
    loop_open = copy.deepcopy(data_tr)
    body = copy.deepcopy(data_tr)
    loop_close = copy.deepcopy(data_tr)
    _cell_text_in_tr(loop_open, 0, "{%tr for r in " + loop_tag + " %}")
    for i in range(1, len(loop_open.findall("w:tc", _NS))):
        _cell_text_in_tr(loop_open, i, "")
    for idx, text in body_cells.items():
        _cell_text_in_tr(body, idx, text)
    _cell_text_in_tr(loop_close, 0, "{%tr endfor %}")
    for i in range(1, len(loop_close.findall("w:tc", _NS))):
        _cell_text_in_tr(loop_close, i, "")
    anchor = orig_rows[header_row_idx]
    anchor.addnext(loop_close)
    anchor.addnext(body)
    anchor.addnext(loop_open)
    # hapus baris contoh ASLI (berdasar identitas elemen - kebal indeks);
    # baris loop adalah deepcopy sehingga tidak mungkin ikut terhapus
    for tr in orig_rows:
        if id(tr) not in keep_ids:
            tr.getparent().remove(tr)


def _delete_between_anchors(doc, start_marker: str, end_marker: str, tag: str,
                            p_level: bool = False) -> None:
    """Ganti konten contoh antara DUA JUDUL (anchor teks) dengan satu paragraf
    tag. Paragraf [awal..sebelum judul akhir] -> {{ tag }}. Aman terhadap
    pergeseran indeks karena selalu dicari ulang dari teks."""
    paras = doc.paragraphs
    start_i = end_i = None
    for i, p in enumerate(paras):
        t = p.text.strip()
        if start_i is None and t.startswith(start_marker):
            start_i = i
        elif start_i is not None and t == end_marker:
            end_i = i
            break
    assert start_i is not None, f"Awal tidak ditemukan: {start_marker!r}"
    assert end_i is not None, f"Akhir tidak ditemukan: {end_marker!r}"
    _tag_para(doc, tag, paras[start_i], p_level=p_level)
    _delete(paras[start_i + 1 : end_i])


def _replace_by_text(doc, marker: str, count: int, tag: str):
    """Temukan paragraf berawalan `marker` lalu ubah N paragraf itu + berikutnya
    menjadi satu paragraf tag {{ tag }}."""
    paras = doc.paragraphs
    for i, p in enumerate(paras):
        if p.text.strip().startswith(marker):
            _tag_para(doc, tag, paras[i])
            for extra in paras[i + 1 : i + count]:
                if extra.text.strip():
                    if _carries_sectpr(extra):
                        _clear_para(extra)
                    else:
                        extra._p.getparent().remove(extra._p)
            return
    raise AssertionError(f"Paragraf acuan tidak ditemukan: {marker!r}")


def _has_picture(para) -> bool:
    xml = para._p.xml
    return "graphic" in xml or "pict" in xml


def build() -> Path:
    assert SOURCE_PATH.exists(), f"Salinan template resmi hilang: {SOURCE_PATH}"
    doc = Document(str(SOURCE_PATH))
    paras = doc.paragraphs

    # ---------- SAMPUL: judul & kode unit di textbox (satu box) ----------
    ok = _replace_textbox(
        paras[20],
        {"PERANGKAT LUNAK": "{{ judul_modul }}", "J.63OPR00.005.2": "{{ kode_unit }}"},
    )
    assert ok, "Textbox sampul tidak ditemukan - struktur template berubah?"

    # ---------- KATA PENGANTAR (contoh isi p42-45, indeks asli aman) ----------
    _replace_sample(doc, 42, 45, "kata_pengantar")

    # ---------- PENDAHULUAN: contoh paragraf ke-2 -> tag (anchor teks) ----------
    _replace_by_text(doc, "Modul pelatihan merupakan buku panduan", 1, "pendahuluan")

    # ---------- SILABUS: tabel identitas (tbl1) ----------
    ident = doc.tables[1]
    _set_cell(ident.rows[0].cells[2], "{{ unit_kompetensi }}")
    _set_cell(ident.rows[1].cells[2], "{{ kode_unit }}")
    _set_cell(ident.rows[2].cells[2], "{{ alokasi_waktu }}")
    _set_cell(ident.rows[3].cells[2], "{{ bentuk_pelatihan }}")
    _set_cell(ident.rows[4].cells[2], "{{ deskripsi_unit }}")

    # ---------- Tabel elemen/KUK (tbl2, 9 kolom) -> loop docxtpl ----------
    tbl2 = doc.tables[2]
    _make_table_loop(
        tbl2,
        header_row_idx=0,
        data_row_idx=1,
        loop_tag="elemen_rows",
        body_cells={
            0: "{{ r.elemen_no }}",
            1: "{{ r.elemen }}",
            2: "{{ r.kuk_no }}",
            3: "{{ r.kuk }}",
            4: "{{ r.indikator }}",
            5: "{{ r.pengetahuan }}",
            6: "",
            7: "{{ r.keterampilan }}",
            8: "{{ r.durasi }}",
        },
        keep_rows=1,
    )

    # ---------- PENGETAHUAN: hapus seluruh contoh isi (anchor teks) ----------
    # Rentang: "Pembelajaran berbasis..." (asli p95) s.d. SEBELUM judul
    # "Evaluasi Pengetahuan" (asli p207). Termasuk gambar contoh (Gambar 1-3).
    _delete_between_anchors(
        doc, "Pembelajaran berbasis Teknologi Informasi", "Evaluasi Pengetahuan",
        "pengetahuan_content", p_level=True,
    )

    # ---------- EVALUASI PENGETAHUAN: contoh soal -> tag ----------
    _replace_by_text(doc, "Pengetahuan tentang pembuatan dokumen", 3, "evaluasi_pengetahuan")

    # ---------- LIK ----------
    for i, p in enumerate(doc.paragraphs):
        if p.text.strip().startswith("Lembar Instruksi Kerja (LIK)_1"):
            _set_text(p, "Lembar Instruksi Kerja (LIK) - No. {{ lik_nomor }} : {{ lik_nama }}")
            break

    # tabel identitas LIK (tbl3): label asli = Unit Kompetensi / Kode Unit /
    # Nama LIK / No. LIK / Waktu
    tbl3 = doc.tables[3]
    _set_cell(tbl3.rows[0].cells[2], "{{ unit_kompetensi }}")
    _set_cell(tbl3.rows[1].cells[2], "{{ kode_unit }}")
    _set_cell(tbl3.rows[2].cells[2], "{{ lik_nama }}")
    _set_cell(tbl3.rows[3].cells[2], "{{ lik_nomor }}")
    _set_cell(tbl3.rows[4].cells[2], "{{ alokasi_waktu }}")

    # skenario contoh -> tag
    _replace_by_text(doc, "Sebagai karyawan anda diminta", 1, "lik_skenario")

    # "Gambar Kerja" -> paragraf berikutnya (berisi gambar contoh) jadi tag
    paras = doc.paragraphs
    for i, p in enumerate(paras):
        if p.text.strip() == "Gambar Kerja":
            _tag_para(doc, "lik_gambar_kerja", paras[i + 1])
            break

    # langkah kerja contoh ("Buatlah.../Editlah.../Cetaklah...") -> tag
    _replace_by_text(doc, "Buatlah File dokumen lembar sebar", 3, "lik_langkah_kerja")

    # ---------- Tabel Bahan Praktik (tbl4, 4 kolom) -> loop ----------
    _make_table_loop(
        doc.tables[4],
        header_row_idx=0,
        data_row_idx=1,
        loop_tag="bahan_rows",
        body_cells={0: "{{ r.no }}", 1: "{{ r.nama }}", 2: "{{ r.spek }}", 3: "{{ r.jumlah }}"},
        keep_rows=1,
    )

    # peralatan contoh (Komputer/Printer/Kabel Rol/Pointer/Obeng) -> tag
    _replace_by_text(doc, "Komputer", 5, "lik_peralatan")

    # ---------- Lembar Cek Observasi (tbl5) & Cek Hasil (tbl6) -> loop ----------
    # tbl5: 2 baris header (merge); data mulai r2 -> loop SETELAH r1
    _make_table_loop(
        doc.tables[5],
        header_row_idx=1,
        data_row_idx=2,
        loop_tag="cek_observasi_rows",
        body_cells={0: "{{ r.langkah }}", 1: "{{ r.acuan }}", 2: "", 3: ""},
        keep_rows=2,
    )
    _make_table_loop(
        doc.tables[6],
        header_row_idx=1,
        data_row_idx=2,
        loop_tag="cek_hasil_rows",
        body_cells={0: "{{ r.no }}", 1: "{{ r.aspek }}", 2: "{{ r.standar }}", 3: "", 4: ""},
        keep_rows=2,
    )

    # evaluasi praktik contoh -> tag
    _replace_by_text(doc, "Praktik dan sikap kerja pembuatan dokumen", 3, "evaluasi_praktik")

    # ---------- LAMPIRAN ----------
    tbl9 = doc.tables[9]
    _set_cell(tbl9.rows[0].cells[2], "{{ kode_unit }}")
    _set_cell(tbl9.rows[1].cells[2], "{{ unit_kompetensi }}")
    _set_cell(tbl9.rows[2].cells[2], "{{ unit_kompetensi_detail }}")

    # tbl10 elemen/kuk lampiran -> loop elemen_rows (2 kolom)
    _make_table_loop(
        doc.tables[10],
        header_row_idx=0,
        data_row_idx=1,
        loop_tag="elemen_rows",
        body_cells={0: "{{ r.elemen }}", 1: "{{ r.kuk }}"},
        keep_rows=1,
    )

    # ---------- Kamus Istilah (tbl7) & Referensi (tbl8) -> loop ----------
    _make_table_loop(
        doc.tables[7],
        header_row_idx=0,
        data_row_idx=0,
        loop_tag="kamus_rows",
        body_cells={0: "{{ r.no }}", 1: "{{ r.istilah }}", 2: ":", 3: "{{ r.arti }}"},
        keep_rows=0,
    )
    _make_table_loop(
        doc.tables[8],
        header_row_idx=0,
        data_row_idx=0,
        loop_tag="referensi_rows",
        body_cells={0: "{{ r.no }}", 1: "{{ r.url }}"},
        keep_rows=0,
    )

    # ---------- BATASAN VARIABEL: contoh p304-326 ----------
    _replace_by_text(doc, "Konteks variabel", 23, "batasan_variabel")

    # ---------- PANDUAN PENILAIAN: contoh ----------
    # Catatan: paragraf terakhir rentang ini MEMBAWA w:sectPr - _replace_by_text
    # mengosongkannya (bukan menghapus) sehingga section ke-6 tetap utuh.
    _replace_by_text(doc, "Konteks penilaian", 21, "panduan_penilaian")

    # ---------- NAMA PENYUSUN ----------
    tbl11 = doc.tables[11]
    _set_cell(tbl11.rows[1].cells[1], "{{ nama_penyusun }}")
    _set_cell(tbl11.rows[1].cells[2], "{{ profesi_penyusun }}")
    _set_cell(tbl11.rows[2].cells[1], "{{ nama_penyusun_2 }}")
    _set_cell(tbl11.rows[2].cells[2], "{{ profesi_penyusun_2 }}")

    # ---------- RONDE 12: daftar isi, heading bab, seragam warna ----------
    # Dijalankan SETELAH semua edit berbasis indeks tabel (menghapus tbl0
    # menggeser indeks doc.tables). Anchor teks di sini aman terhadap urutan.
    _apply_heading_styles(doc)
    _normalize_colors(doc)
    _replace_toc(doc)
    _enable_update_fields(doc)

    doc.save(str(TEMPLATE_PATH))
    print(f"[template_builder] Template tersimpan -> {TEMPLATE_PATH}")
    return TEMPLATE_PATH


if __name__ == "__main__":
    build()
    from tools.doc_utils import get_template_tags

    tags = get_template_tags()
    print(f"[template_builder] Terdeteksi {len(tags)} tag: {tags}")
    # 24 tag string: sampul(2) + pengantar/pendahuluan(2) + ident(5) +
    # pengetahuan(2) + lik(5) + evaluasi praktik(1) + lampiran(3) +
    # batasan/penilaian(2) + penyusun(4) - lik_bahan/lik_cek_* /kamus/referensi
    # kini berbentuk ROW-LOOP (bukan tag string)
    assert len(tags) >= 24, f"Tag kurang: {len(tags)}"
    from docxtpl import DocxTemplate

    tpl = DocxTemplate(str(TEMPLATE_PATH))
    rows = [
        {
            "elemen_no": "1.",
            "elemen": "E",
            "kuk_no": "1.1",
            "kuk": "K",
            "indikator": "I",
            "pengetahuan": "P",
            "keterampilan": "S",
            "durasi": "2 JP",
        }
    ]
    tpl.render(
        {
            "elemen_rows": rows,
            "bahan_rows": [{"no": "1", "nama": "N", "spek": "S", "jumlah": "J"}],
            "cek_observasi_rows": [{"langkah": "L", "acuan": "A"}],
            "cek_hasil_rows": [{"no": "1", "aspek": "A", "standar": "S"}],
            "kamus_rows": [{"no": "a.", "istilah": "I", "arti": "A"}],
            "referensi_rows": [{"no": "a.", "url": "U"}],
        }
    )
    print("[template_builder] Render uji docxtpl sukses - template VALID.")