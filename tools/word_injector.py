"""
WORD INJECTOR - Rakit draft_json menjadi file .docx fisik
=========================================================
Contoh penggunaan library docxtpl:

    from docxtpl import DocxTemplate

    tpl = DocxTemplate("template_kemnaker.docx")  # template berisi {{ judul_modul }} dll.
    tpl.render({"judul_modul": "Instalasi PLTS", "konten": "..."})
    tpl.save("output/hasil.docx")

Modul ini memakai template resmi Kemnaker (database/template_kemnaker.docx
- dibangun dari Templet Modul_2024 resmi, lihat tools/template_builder.py).
Tag yang tidak diisi LLM diberi nilai default "--- (perlu dilengkapi
reviewer) ---" agar render tidak error dan reviewer bisa melihat bagian
mana yang bolong.

Ronde 11 (kesesuaian template + gambar wajib):
  * pengetahuan_content dirakit via docxtpl Subdoc -> paragraf Word ASLI:
    baris bergaya judul subbab ("1. ..." / "1.1 ...") dibuat BOLD, blok
    ```mermaid ... ``` dirender jadi GAMBAR PNG (tools/mermaid_render.py)
    dan disisipkan sebagai gambar - sesuai tuntutan user bahwa flowchart
    tidak boleh hilang dari .docx.
  * lik_gambar_kerja yang berisi blok mermaid juga dirender jadi gambar
    (docxtpl InlineImage); jika bukan skrip mermaid, dimasukkan sebagai teks.
  * key row-loop docxtpl (elemen_rows, bahan_rows, cek_observasi_rows,
    cek_hasil_rows, kamus_rows, referensi_rows) dinormalkan: baris yang
    kurang key dilengkapi string kosong agar render tak pernah Undefined.
"""

import copy
import os
import re
from pathlib import Path
from typing import List, Optional

from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Mm, Pt
from docxtpl import DocxTemplate, InlineImage, Listing
from docxtpl.subdoc import Subdoc

from agents.state import GlobalState, ModuleState
from tools.doc_utils import (
    OUTPUT_DIR,
    TEMPLATE_PATH,
    cap_first,
    get_template_tags,
    pengetahuan_items,
    to_active_voice,
)
from tools.mermaid_render import render_mermaid

# Ronde 13: key draft BUKAN tag template - query pencarian gambar internet
# yang ditulis Agent 2 (dieksekusi tools/image_search.py saat injeksi).
IMAGE_QUERY_KEYS = ("cover_image_query", "image_queries")

# Nilai default untuk tag template yang tidak terisi LLM
DEFAULT_MISSING = "--- (perlu dilengkapi reviewer) ---"

# Key row-loop docxtpl ({%tr %}) - tidak terdeteksi regex tag sederhana
ROW_KEYS = (
    "elemen_rows",
    "bahan_rows",
    "cek_observasi_rows",
    "cek_hasil_rows",
    "kamus_rows",
    "referensi_rows",
)

# Key wajib tiap baris loop - dilengkapi "" agar render tidak Undefined
_ROW_REQUIRED = {
    "elemen_rows": ("elemen_no", "elemen", "kuk_no", "kuk", "indikator",
                    "pengetahuan", "keterampilan", "durasi"),
    "bahan_rows": ("no", "nama", "spek", "jumlah"),
    "cek_observasi_rows": ("langkah", "acuan"),
    "cek_hasil_rows": ("no", "aspek", "standar"),
    "kamus_rows": ("no", "istilah", "arti"),
    "referensi_rows": ("no", "url"),
}

_HEADING_RE = re.compile(r"^\d+(\.\d+)*\.?\s+\S")  # "1. Judul" / "1.1 Judul"
_MERMAID_RE = re.compile(r"```mermaid\s*(.*?)```", re.DOTALL)


def _slugify(text: str) -> str:
    """Judul modul -> nama file yang aman (tanpa karakter ilegal Windows)."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")[:60] or "modul"


def _strip_heading(heading: str) -> str:
    """Hapus nomor di awal judul subbab: "1. Merakit Panel Surya" ->
    "Merakit Panel Surya". Dipakai ronde 16 untuk judul caption & fallback
    query gambar bila Agent 2 tidak menyediakan entri foto utk subbab itu."""
    return re.sub(r"^\d+(\.\d+)*\.?\s+", "", heading or "").strip()


def _render_mermaid_safe(code: str, out_path: Path) -> Optional[Path]:
    """Render skrip mermaid -> PNG; None kalau gagal (jangan pernah crash
    injection karena skrip mermaid yang tidak sempurna)."""
    try:
        return render_mermaid(code, out_path)
    except Exception as exc:  # noqa: BLE001
        print(f"[word_injector] Mermaid gagal dirender ({exc}) - lewati gambar")
        return None


def _fit_png(png: Optional[Path], max_w_mm: float,
             max_h_mm: float) -> Optional[float]:
    """Lebar tampilan (mm) gambar PNG agar PAS KOTAK max_w x max_h mm
    (ronde 12, komentar reviewer: "terlalu besar, tolong dikecilkan dan
    disesuaikan"). Ukuran piksel dibaca via PIL; rasio aspek dipertahankan
    dan gambar tidak pernah melebihi kedua batas. None bila PNG tak terbaca."""
    if png is None:
        return None
    try:
        from PIL import Image

        with Image.open(str(png)) as im:
            px_w, px_h = im.size
    except Exception:  # noqa: BLE001 - PIL tak terbaca: pakai lebar aman
        return min(max_w_mm, 120.0)
    if px_w <= 0 or px_h <= 0:
        return min(max_w_mm, 120.0)
    aspect = px_w / px_h
    return round(min(max_w_mm, max_h_mm * aspect), 1)


# Batas ukuran gambar di docx final (mm) - kertas A4 margin template:
# area teks ~150mm. Contoh modul memakai gambar 43-129mm; gambar raksasa
# 145x350mm/150x594mm pada output sebelumnya dikritik reviewer.
_IMG_PENGETAHUAN_MAX = (135.0, 100.0)  # diagram flowchart pengetahuan
_IMG_GAMBAR_KERJA_MAX = (120.0, 100.0)  # gambar kerja LIK

# Slot foto cover DI DALAM template (ronde 18). Bukan gambar yang disisipkan
# kode - ia sudah ada di template sebagai anchor behindDoc 205.9x144.1 mm
# (rasio 10:7), jadi byte-nya cukup DITUKAR. Lihat _apply_generated_cover().
_COVER_ENTRY = "word/media/image3.jpg"


def _image_mode(llm_mode: str) -> str:
    """Kebijakan sumber gambar. SELALU salah satu dari "replicate" | "search".

    Dari env IMAGE_MODE (ronde 18):
    - "replicate" : Replicate dulu utk SETIAP gambar (permintaan user: "all
      the image are all generated with replicate API"); pencarian internet
      hanya jaring pengaman terakhir agar subbab tak kosong.
    - "search"    : foto internet dulu, Replicate sbg fallback.
    - "auto"      : perilaku ronde 13b - ikuti mode per-query dari Agent 2
      (mode "generate" -> Replicate dulu, selain itu cari internet dulu).

    Return HANYA dua nilai: pemanggil membandingkan dgn "replicate". Jangan
    pernah mengembalikan "generate" - pemanggil lama membandingkan ke nilai
    itu dan mode replicate akan diam-diam jadi "cari internet dulu" (bug
    ronde 18: IMAGE_MODE=replicate tak berpengaruh sama sekali).
    """
    env = (os.getenv("IMAGE_MODE") or "auto").strip().lower()
    if env in ("replicate", "search"):
        return env
    return "replicate" if str(llm_mode or "").strip() == "generate" else "search"


def _subbab_images_enabled() -> bool:
    """Kill switch gambar per SUB-SUBBAB (ronde 18, WS4).

    Tiap gambar sub-subbab = satu panggilan Replicate berbayar, jadi biaya
    produksi naik sebanding jumlah sub-subbab. Default AKTIF ("1"); set
    IMAGE_SUBBAB_ENABLED=0 untuk kembali ke perilaku ronde 16 (satu gambar
    per subbab elemen saja) tanpa mengubah kode.
    """
    return (os.getenv("IMAGE_SUBBAB_ENABLED", "1") or "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _fetch_image_safe(query: str, out_path: Path, mode: str = "cari") -> Optional[Path]:
    """Gambar utk query. Urutan sumber ditentukan `_image_mode()` (IMAGE_MODE).

    Selalu return path atau None - kegagalan internet/API TIDAK boleh
    menggagalkan injeksi Word. Jaring pengaman terakhir dipertahankan
    (ronde 16: "tiap subbab dijamin punya gambar")."""
    query = str(query or "").strip()
    if not query:
        return None

    def _generate() -> Optional[Path]:
        try:
            from tools.image_gen import generate_image, subbab_output_format

            png = generate_image(query, out_path,
                                 output_format=subbab_output_format())
            if png:
                return Path(png)
        except Exception as exc:  # noqa: BLE001
            print(f"[word_injector] Generator Replicate gagal ({exc}) - lewati")
        return None

    def _search() -> Optional[Path]:
        try:
            from tools.image_search import fetch_image

            got = fetch_image(query, out_path)
            if got:
                print(f"[word_injector] Gambar internet OK ({got.get('source')}): "
                      f"{query[:60]}")
                return Path(got["path"])
        except Exception as exc:  # noqa: BLE001
            print(f"[word_injector] Pencari gambar gagal ({exc}) - lewati")
        return None

    if _image_mode(mode) == "replicate":
        got = _generate() or _search()
    else:
        got = _search() or _generate()
    if got is None:
        # Kedua sumber gagal -> slot gambar DIBIARKAN KOSONG (injeksi .docx
        # tidak boleh gagal karena generator gambar). Jejaknya dulu tidak
        # lengkap: log memuat "Replicate gagal ... - lewati" tetapi tidak
        # pernah menyatakan bahwa subbabnya benar-benar terbit TANPA gambar,
        # dan `image_search.fetch_image` yang pulang kosong tidak mencetak
        # apa pun. Terukur pada smoke ronde 18: satu ReadTimeout + pencarian
        # yang tak menemukan apa pun = subbab "1. Memeriksa dokumen
        # pengiriman sampah" tampil tanpa gambar, tanpa satu baris pun yang
        # menunjuk ke sana.
        print(f"[word_injector] PERINGATAN: tidak ada gambar utk "
              f"\"{query[:60]}\" (Replicate + pencarian gagal) - "
              f"slot dibiarkan kosong")
    return got


def _normalize_image_queries(value) -> dict:
    """draft_json["image_queries"] -> {"<no subbab/subbab>": {"query","judul"}}.
    Bentuk mentah dari LLM: list of {"subbab": "1", "query": "...",
    "judul": "..."}. Entri rusak dibuang - sisanya tetap dipakai.

    Ronde 18 (WS4): key boleh nomor SUB-SUBBAB ("1.1", "1.4") selain subbab
    elemen ("1") - dipakai _maybe_photo untuk menyisipkan gambar alat/APD
    tepat di bawah tiap sub-subbab pengetahuan (komentar reviewer id=7).
    """
    out: dict = {}
    if not isinstance(value, list):
        return out
    for item in value:
        if not isinstance(item, dict):
            continue
        query = str(item.get("query") or "").strip()
        if not query:
            continue
        raw = str(item.get("subbab") or item.get("no") or "").strip().rstrip(".")
        # "1.0" (hasil konversi angka oleh LLM) -> "1"; "1.10" TIDAK ikut jadi
        # "1.1" (dulu regex `\.0$` menggerusnya - tabrakan key).
        num = re.sub(r"^(\d+)\.0$", r"\1", raw)
        if not num:
            continue
        out[num] = {
            "query": query,
            "judul": str(item.get("judul") or query).strip(),
            # ronde 13b: "cari" (foto internet, default) | "generate" (AI Replicate)
            "mode": str(item.get("mode") or "cari").strip() or "cari",
        }
    return out


def _rewrite_zip_entries(docx_path: Path, replacements: dict) -> bool:
    """Tulis ulang .docx dengan ISI beberapa entry diganti; entry lain apa adanya.

    Semua entry ditulis ulang dalam URUTAN ASLI dan memakai `zin.getinfo(name)`
    sebagai ZipInfo sehingga date_time / compress_type / external_attr tiap
    entry ikut terjaga, dan [Content_Types].xml tetap jadi entry pertama (tidak
    pernah ditulis ulang, jadi tidak mungkin rusak).

    `replacements` = {nama_entry: bytes_baru}; entry yang tidak ada di zip
    dilewati (dicatat) tanpa menggagalkan sisanya.

    Catatan: "apa adanya" berlaku utk ISI entry; stream deflate-nya
    di-encode ulang oleh zipfile (Word tidak mempermasalahkan ini).
    """
    import zipfile

    if not replacements:
        return False
    docx_path = Path(docx_path)
    tmp = docx_path.with_name(docx_path.name + ".swap")
    try:
        with zipfile.ZipFile(docx_path) as zin:
            names = zin.namelist()
            missing = [e for e in replacements if e not in names]
            if missing:
                print(f"[word_injector] Entry tidak ada di {docx_path.name}: {missing}")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
                for name in names:  # urutan asli - jangan di-sort
                    info = zin.getinfo(name)
                    zout.writestr(info, replacements.get(name) or zin.read(name))
                zout.comment = zin.comment
        os.replace(str(tmp), str(docx_path))
        return True
    except Exception as exc:  # noqa: BLE001 - gagal normalisasi != injeksi gagal
        print(f"[word_injector] Tulis ulang entry gagal ({type(exc).__name__}: "
              f"{str(exc)[:120]}) - dokumen dibiarkan apa adanya")
        return False
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _swap_zip_entry(docx_path: Path, entry: str, new_bytes: bytes) -> bool:
    """Ganti isi SATU entry zip; seluruh entry lain disalin apa adanya.

    Dipakai utk menukar foto cover template (ronde 18) - pembungkus tipis
    _rewrite_zip_entries agar pemanggil lama tidak berubah.
    """
    return _rewrite_zip_entries(Path(docx_path), {entry: new_bytes})


# ----------------------------------------------------------------------
# Ronde 18 (WS3b): normalisasi pasca-render
# ----------------------------------------------------------------------
# Dua cacat yang HANYA lahir saat render (template tidak bisa memperbaikinya):
#
#  1. NUMBERING HILANG (komentar reviewer id=22 "Seharusnya ada numbering").
#     docxtpl.Listing mengubah "\n" menjadi <w:br/> DI DALAM SATU paragraf
#     (docxtpl/template.py: resolve_listing). Word hanya menomori baris
#     pertama; sisanya tampak tanpa nomor. Perbaikan: pecah paragraf ber-numPr
#     yang memuat <w:br/> menjadi N paragraf dengan pPr yang SAMA.
#
#  2. NUMBERING KEDOUBLE (id=44 "Numbering ny kedouble"). LLM kadang menulis
#     "1. Melaksanakan tugas..." padahal paragrafnya sudah ber-numPr
#     (lvlText "%1.") -> tampil "1. 1. ...". Perbaikan: buang prefiks ordinal
#     dari run pertama paragraf ber-numPr. Deterministik, jadi tetap benar
#     walau LLM membandel.
#
# Ditambah jaring pengaman warna: 00B0F0 -> 000000 kalau template suatu saat
# dirombak lagi tanpa _normalize_colors.

_ORDINAL_RE = re.compile(r"^\s*\d+[.)]\s+")
_LINE_BREAK_TYPES = (None, "", "textWrapping")
_BLUE = "00B0F0"


def _is_line_break(br) -> bool:
    """True hanya utk <w:br/> yang memindah baris - BUKAN page break.
    Page break (w:type="page"/"column") harus dipertahankan apa adanya."""
    return (br.get(qn("w:type")) or "").strip() in _LINE_BREAK_TYPES


def _has_numpr(p) -> bool:
    pPr = p.find(qn("w:pPr"))
    return pPr is not None and pPr.find(qn("w:numPr")) is not None


def _split_para_on_breaks(p) -> bool:
    """Pecah satu <w:p> BER-numPr pada tiap <w:br/> menjadi paragraf terpisah
    dengan pPr + rPr yang sama. Return True bila paragraf benar-benar dipecah.

    Sengaja HANYA paragraf ber-numPr: <w:br/> di paragraf biasa (prosa) adalah
    pemindahan baris yang disengaja dan tampilannya sudah benar - memecahnya
    justru menambah jarak `w:after` dan mengubah perilaku keepNext/daftar isi.
    """
    if not _has_numpr(p):
        return False
    breaks = [br for br in p.findall(".//" + qn("w:br")) if _is_line_break(br)]
    if not breaks:
        return False

    pPr = p.find(qn("w:pPr"))
    segments = [[]]  # list[list[OxmlElement]] - isi tiap paragraf baru
    for child in list(p):
        if child is pPr:
            continue
        if child.tag != qn("w:r"):
            segments[-1].append(child)  # elemen non-run: ikut segmen berjalan
            continue
        rPr = child.find(qn("w:rPr"))
        parts = [[]]
        for rc in list(child):
            if rc is rPr:
                continue
            if rc.tag == qn("w:br") and _is_line_break(rc):
                parts.append([])
            else:
                parts[-1].append(rc)
        for i, part in enumerate(parts):
            if i > 0:
                segments.append([])
            if not part:
                continue  # segmen kosong (mis. dua <w:br/> berurutan)
            new_run = OxmlElement("w:r")
            if rPr is not None:
                new_run.append(copy.deepcopy(rPr))  # rPr disalin ke TIAP bagian
            for rc in part:
                new_run.append(rc)  # node asli dipindah, bukan disalin
            segments[-1].append(new_run)

    if len(segments) <= 1:
        return False  # hanya <w:br/> tanpa teks sesudahnya - tak ada yang dipecah

    parent = p.getparent()
    for seg in segments:
        new_p = OxmlElement("w:p")
        if pPr is not None:
            new_p.append(copy.deepcopy(pPr))  # numPr ikut -> tiap baris bernomor
        for el in seg:
            new_p.append(el)
        p.addprevious(new_p)
    parent.remove(p)
    return True


def _strip_ordinal_prefix(p) -> bool:
    """Buang prefiks ordinal ("1. " / "2) ") dari awal paragraf ber-numPr.

    Prefiks TIDAK selalu berada di satu `w:t`: Agent 2/LLM menulisnya sebagai
    run sendiri (`<w:t>1) </w:t><w:t>Pengetahuan tentang...</w:t>`), jadi teks
    seluruh node `w:t` digabung dulu, panjang prefiks dihitung dari gabungan
    itu, lalu dipotong dari node-node di depan secara berurutan. Node yang
    habis terpotong jadi kosong - itu benar, karena nomornya kini dibuat Word.
    """
    if not _has_numpr(p):
        return False
    ts = list(p.findall(".//" + qn("w:t")))
    joined = "".join(t.text or "" for t in ts)
    if not joined.strip():
        return False
    m = _ORDINAL_RE.match(joined)
    if m is None:
        return False  # tidak ada prefiks ordinal -> tidak kedouble

    drop = m.end()
    for t in ts:
        if drop <= 0:
            break
        txt = t.text or ""
        if len(txt) <= drop:
            drop -= len(txt)
            t.text = ""
        else:
            t.text = txt[drop:]
            drop = 0
        t.set(qn("xml:space"), "preserve")
    return True


def _normalize_rendered_docx(docx_path: Path) -> bool:
    """Perbaiki NUMBERING (hilang/kedouble) + sisa warna biru pada .docx hasil
    render. Dijalankan pada file .tmp SEBELUM dipublikasikan, jadi pembaca
    tidak pernah melihat versi setengah dinormalisasi."""
    from docx.oxml import parse_xml

    docx_path = Path(docx_path)
    try:
        import zipfile

        with zipfile.ZipFile(docx_path) as z:
            doc_xml = z.read("word/document.xml")
            try:
                num_xml = z.read("word/numbering.xml")
            except KeyError:
                num_xml = None
    except Exception as exc:  # noqa: BLE001
        print(f"[word_injector] Normalisasi dilewati ({type(exc).__name__}: {exc})")
        return False

    root = parse_xml(doc_xml)
    split_n = strip_n = 0
    # DUA fase: pecah dulu SEMUA paragraf ber-<w:br/> (daftar yang di-Listing),
    # baru sapu prefiks ordinal di SELURUH pohon termasuk paragraf hasil
    # pemecahan. Satu fase tidak cukup: paragraf yang dipecah dilewati, dan
    # justru di situ ordinal kedouble paling sering muncul ("1. Menerima
    # instruksi kerja<w:br/>2. Mencatat ..." -> dua nomor palsu).
    for p in list(root.iter(qn("w:p"))):
        if _split_para_on_breaks(p):
            split_n += 1
    for p in list(root.iter(qn("w:p"))):
        if _strip_ordinal_prefix(p):
            strip_n += 1

    num_root = parse_xml(num_xml) if num_xml else None
    blue_n = 0
    for root_el in (root, num_root):
        if root_el is None:
            continue
        for color_el in root_el.findall(".//" + qn("w:color")):
            if (color_el.get(qn("w:val")) or "").upper() == _BLUE:
                color_el.set(qn("w:val"), "000000")
                blue_n += 1

    replacements = {"word/document.xml": _serialize_xml(root)}
    if num_root is not None:
        replacements["word/numbering.xml"] = _serialize_xml(num_root)
    ok = _rewrite_zip_entries(docx_path, replacements)
    if ok:
        print(f"[word_injector] Normalisasi: {split_n} paragraf dipecah (numbering "
              f"per baris), {strip_n} prefiks ordinal dibuang, {blue_n} warna biru "
              f"-> hitam")
    return ok


def _serialize_xml(root) -> bytes:
    """Serialisasi elemen OOXML kembali menjadi bytes UTF-8 + deklarasi XML."""
    from lxml import etree

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                          standalone=True)


def _apply_generated_cover(docx_path: Path, draft_json: dict,
                           module_title: str) -> bool:
    """Ganti foto cover template dengan ilustrasi hasil Replicate (ronde 18).

    Slot cover template = `word/media/image3.jpg` (1400x980 px, rasio 10:7),
    dirujuk PERSIS SATU KALI sebagai anchor behindDoc 205.9x144.1 mm di
    belakang textbox judul. Karena byte-nya ditukar (bukan ditambah gambar
    baru), tata letak resmi Kemnaker - judul, kode unit, footer - tidak
    bergeser sedikit pun, dan gambar dijamin terlihat (sblmnya overlay
    pernah menutupi gambar template dan ditolak reviewer).

    Return True bila cover berhasil diganti. Semua kegagalan (token kosong,
    kredit habis, Word sedang membuka file) -> False: cover template tetap
    dipakai, dokumen tetap tersimpan.
    """
    if (os.getenv("IMAGE_COVER_MODE", "replicate") or "").strip().lower() != "replicate":
        return False

    query = str((draft_json or {}).get("cover_image_query") or "").strip() or \
        str(module_title or "").strip()
    if not query:
        return False

    try:
        from tools.image_gen import generate_cover

        tmp_jpg = Path(docx_path).parent / "_mermaid" / "cover.jpg"
        jpg = generate_cover(query, tmp_jpg)
        if jpg is None:
            print("[word_injector] Cover Replicate tidak tersedia - pakai foto template")
            return False
        if _swap_zip_entry(Path(docx_path), _COVER_ENTRY, Path(jpg).read_bytes()):
            print(f"[word_injector] Cover diganti ilustrasi Replicate: {query[:60]}")
            return True
    except Exception as exc:  # noqa: BLE001 - jangan gagalkan injeksi Word
        print(f"[word_injector] Cover generate gagal ({type(exc).__name__}) - lewat")
    return False


def _canonical_subbab(syllabus_rows) -> tuple:
    """Judul subbab/sub-subbab LEGAL dari silabus modul (set ternormalisasi).
    Ronde 12: HANYA baris yang persis redaksi elemen/butir pengetahuan
    silabus boleh jadi heading - baris list bernomor di tengah paragraf
    ("1. Sambut truk...") tidak boleh masuk daftar isi Word."""
    subs, subsubs = set(), set()
    for r in syllabus_rows or []:
        if not isinstance(r, dict):
            continue
        try:
            n = int(str(r.get("elemen_no", "")).strip().rstrip("."))
        except ValueError:
            continue
        subs.add(re.sub(r"\s+", " ", f"{n}. {r.get('elemen', '')}".strip().lower()).strip(" .;"))
        for i, item in enumerate(
                pengetahuan_items(str(r.get("pengetahuan") or "")), start=1):
            item_clean = cap_first(item.strip())
            subsubs.add(re.sub(r"\s+", " ", f"{n}.{i} {item_clean}".lower()).strip(" .;"))
    return subs, subsubs


def _build_pengetahuan_subdoc(tpl: DocxTemplate, text: str, tmp_dir: Path,
                              syllabus_rows=None, photo_queries=None):
    """Rakit nilai pengetahuan_content menjadi Subdoc berisi paragraf asli:
    judul subbab -> style Heading 2 (masuk daftar isi Word); blok
    ```mermaid``` -> gambar PNG terukur + CAPTION "Gambar N. <judul subbab>"
    (ronde 12, komentar reviewer: gambar/flowchart wajib berjudul).

    Ronde 13: photo_queries = {"<no subbab>": {"query","judul"}} dari
    Agent 2 - SETELAH tiap judul subbab disisipkan FOTO nyata dari internet
    (query dieksekusi tools/image_search.py) dengan caption yang memakai
    SATU urutan "Gambar N." bersama diagram mermaid."""
    sub = Subdoc(tpl)
    canon_subs, canon_subsubs = _canonical_subbab(syllabus_rows)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    pos = 0
    img_i = 0  # SATU urutan "Gambar N." utk diagram + foto (ronde 13)
    seen_queries: set = set()  # query yang sudah dipakai -> tidak dobel gambar

    def _add_caption(judul: str) -> None:
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        cap = sub.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cap_run = cap.add_run(f"Gambar {img_i}. {judul}")
        cap_run.font.name = "Bookman Old Style"
        cap_run.font.size = Pt(11)

    def _add_picture_centered(png, max_box, caption=None) -> bool:
        """Gambar pada PARAGRAF SENDIRI, rata-tengah (ronde 16: posisi & lebar
        konsisten, bukan inline di paragraf heading yang rata-kiri), dengan
        caption "Gambar N." opsional. Return True bila gambar berhasil
        dipasang; pemanggil mengelola counter `img_i`."""
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        width_mm = _fit_png(png, *max_box)
        if not width_mm:
            return False
        p = sub.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run().add_picture(str(png), width=Mm(width_mm))
        if caption:
            _add_caption(caption)
        return True

    def _maybe_photo(heading: str) -> None:
        """Foto/ilustrasi ter-center tepat di bawah judul subbab/subbab.

        Komentar reviewer ronde 13: "masih ga ada gambar" - diagram saja tidak
        cukup. Ronde 16: tiap SUBBAB dijamin punya gambar, rata-tengah, ukuran
        konsisten. Ronde 18 (WS4, komentar reviewer id=7 "lebih banyak gambar
        yang menjelaskan keperluan atau alat kerja, seperti APD, alat tulis,
        kertas, contoh instruksi kerja"): gambar juga disisipkan per
        SUB-SUBBAB pengetahuan, memakai entri `image_queries` ber-key "1.1".

        Query sub-subbab yang tidak punya entri eksplisit memakai judul
        sub-subbab itu sendiri (bukan entri induknya) - supaya foto induk
        tidak muncul dua kali berturut-turut, karena subbabnya sudah dapat
        foto sendiri. Matikan lewat IMAGE_SUBBAB_ENABLED=0.

        Counter "Gambar N." hanya naik saat foto BENAR-BENAR terpasang - kalau
        tidak, caption mermaid mulai dari "Gambar 2." tanpa "Gambar 1." (bug
        terukur pada smoke ronde 13).
        """
        nonlocal img_i
        m = re.match(r"^(\d+(?:\.\d+)*)\.?\s", heading or "")
        if not m:
            return
        key = m.group(1)
        is_subsub = "." in key
        if is_subsub and not _subbab_images_enabled():
            return
        spec = (photo_queries or {}).get(key)
        # Subbab tanpa entri query tetap wajib punya gambar (ronde 16); untuk
        # sub-subbab, judulnya sendiri jadi query.
        query = (spec or {}).get("query") or _strip_heading(heading)
        if not query:
            return
        norm_q = re.sub(r"\s+", " ", query.strip().lower())
        if norm_q in seen_queries:
            return  # query kembar -> jangan tempel foto yang sama dua kali
        png = _fetch_image_safe(
            query, tmp_dir / f"foto_{key.replace('.', '_')}.png",
            mode=str((spec or {}).get("mode") or "cari"),
        )
        if png is not None:
            seen_queries.add(norm_q)
            img_i += 1
            judul = ((spec or {}).get("judul") or (spec or {}).get("query")
                     or _strip_heading(heading) or "Gambar kerja")
            _add_picture_centered(png, _IMG_PENGETAHUAN_MAX, judul)

    def _add_lines(chunk: str) -> None:
        nonlocal last_heading
        for line in chunk.split("\n"):
            new_heading = _subdoc_add_line(sub, line, last_heading,
                                           canon_subs, canon_subsubs)
            if new_heading != last_heading and new_heading:
                last_heading = new_heading
                _maybe_photo(last_heading)

    last_heading = ""
    for m in _MERMAID_RE.finditer(text):
        # teks sebelum blok mermaid
        _add_lines(text[pos : m.start()])
        code = m.group(1)
        img_i += 1   # reserve nomor "Gambar N." walau render gagal berhasil
        png = _render_mermaid_safe(code, tmp_dir / f"mermaid_{img_i}.png")
        if png is not None:
            # Ronde 16: diagram tetap di posisi inline tempat penulisan (di
            # dalam urutan teks subbab), tetapi di-center + caption + lebar
            # konsisten (sebelumnya rata-kiri inline di paragraf teks).
            judul = _strip_heading(last_heading) or "Diagram alur kerja"
            if not _add_picture_centered(png, _IMG_PENGETAHUAN_MAX, judul):
                # PIL tak bisa membaca dimensi -> tetap center, lebar tetap.
                from docx.enum.text import WD_ALIGN_PARAGRAPH

                p = sub.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.add_run().add_picture(str(png), width=Inches(5.7))
                _add_caption(judul)
        else:
            # renderer gagal -> simpan skripnya sebagai teks agar tidak hilang
            for line in ("```mermaid" + code + "```").split("\n"):
                _subdoc_add_line(sub, line, last_heading,
                                 canon_subs, canon_subsubs)
        pos = m.end()
    _add_lines(text[pos:])
    return sub


def _subdoc_add_line(sub, line: str, last_heading: str = "",
                     canon_subs=frozenset(), canon_subsubs=frozenset()) -> str:
    """Satu baris -> satu paragraf Subdoc. Return judul subbab terakhir
    terbaru (untuk caption gambar).

    - Baris "N. <redaksi elemen silabus PERSIS>" -> style Heading 2 + run
      Bookman 12 bold (masuk daftar isi TOC \\o "1-2"). Baris bernomor lain
      (list di tengah paragraf) tetap teks biasa - jangan memenuhi TOC.
    - Baris "N.M <redaksi pengetahuan PERSIS>" -> paragraf BOLD (sub-subbab,
      tidak memenuhi TOC).
    - Baris isi -> paragraf justify Bookman 12.
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    line = line.rstrip()
    if not line.strip():
        return last_heading  # paragraf kosong tidak diperlukan
    stripped = line.strip()
    m = re.match(r"^(\d+(?:\.\d+)*)\.?\s+(.*\S)$", stripped)
    if m:
        # titik pemisah habis dimakan regex pada "1. judul" - pulihkan agar
        # key cocok dengan canonical "1. judul" (subbab) dan "1.1 judul"
        num = m.group(1) if "." in m.group(1) else m.group(1) + "."
        key = re.sub(r"\s+", " ", f"{num} {m.group(2)}".strip().lower())
        if "." in m.group(1):
            if key in canon_subsubs:
                p = sub.add_paragraph()
                run = p.add_run(stripped)
                run.bold = True
                run.font.name = "Bookman Old Style"
                run.font.size = Pt(12)
                return stripped
        elif key in canon_subs:
            p = sub.add_paragraph(style="Heading 2")
            run = p.add_run(stripped)
            run.bold = True
            run.font.name = "Bookman Old Style"
            run.font.size = Pt(12)
            return stripped
    p = sub.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    run = p.add_run(stripped)
    run.font.name = "Bookman Old Style"
    run.font.size = Pt(12)
    return last_heading


def _normalize_rows(key: str, value) -> list:
    """Pastikan row-loop selalu list of dict dengan key lengkap.

    Ronde 17: untuk tabel elemen (berisi kolom Indikator + Keterampilan &
    Sikap), terapkan `to_active_voice` secara deterministik pd TITIK INJEKSI —
    jaminan dokumen FINAL: sel keterampilan selalu berawal verba aktif pasangan
    indikator (atau terisi dari pasangan indikator bila sel kosong/placeholder),
    apa pun jalur upstream (LLM, fallback, docx-backed). Idempoten terhadap
    transformasi Agent 1/2."""
    if not isinstance(value, list):
        return []
    required = _ROW_REQUIRED.get(key, ())
    apply_verbs = key == "elemen_rows"
    rows = []
    for row in value:
        if isinstance(row, dict):
            clean = {k: ("" if v is None else str(v)) for k, v in row.items()}
            for k in required:
                clean.setdefault(k, "")
            if apply_verbs:
                clean["keterampilan"] = to_active_voice(
                    clean.get("keterampilan", ""),
                    clean.get("indikator", ""),
                )
            rows.append(clean)
    return rows


def inject_module(module: ModuleState, output_dir: Path = None) -> Path:
    """Render SATU modul menjadi file .docx. Return path hasil.

    Raises:
        RuntimeError: jika template tidak ditemukan / draft kosong.
    """
    if not TEMPLATE_PATH.exists():
        raise RuntimeError(f"Template tidak ditemukan: {TEMPLATE_PATH}")

    draft_json = module.get("draft_json") or {}
    if not draft_json:
        raise RuntimeError(f'Modul {module.get("module_id", "?")} tidak punya draft_json.')

    out_dir = Path(output_dir) if output_dir else OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- Injeksi dictionary ke template ---
    tpl = DocxTemplate(str(TEMPLATE_PATH))

    # Pre-fill SEMUA tag template dengan nilai default -> render tidak error,
    # dan reviewer bisa melihat bagian mana yang belum terisi.
    context = {tag: DEFAULT_MISSING for tag in get_template_tags()}

    # Timpa dengan hasil Agent 2 (hanya nilai non-kosong yang menimpa default).
    # String multi-baris dibungkus docxtpl.Listing agar newline menjadi paragraf
    # asli Word (bukan karakter newline mentah yang diruntuhkan).
    for key, value in draft_json.items():
        if key in ROW_KEYS:
            rows = _normalize_rows(key, value)
            context[key] = rows  # loop kosong -> tabel hanya header (aman)
        elif isinstance(value, str) and value.strip():
            context[key] = Listing(value) if "\n" in value.strip() else value
        elif value:  # nilai non-string lain: lempar mentah
            context[key] = value

    # Ronde 11: pengetahuan_content & lik_gambar_kerja perlakuan khusus
    # (paragraf bold + gambar mermaid) agar .docx final punya DIAGRAM.
    tmp_dir = out_dir / "_mermaid"
    content = str(draft_json.get("pengetahuan_content") or "").strip()
    # Judul subbab legal diambil dari silabus state; bila kosong (modul lama/
    # demo) - turunkan dari elemen_rows draf (strip nomor pada kolom elemen).
    syllabus_rows = module.get("syllabus_rows") or [
        {**r, "elemen": re.sub(r"^\d+\.\s*", "", str(r.get("elemen", "")))}
        for r in (draft_json.get("elemen_rows") or []) if isinstance(r, dict)
    ]
    if content:
        try:
            context["pengetahuan_content"] = _build_pengetahuan_subdoc(
                tpl, content, tmp_dir, syllabus_rows=syllabus_rows,
                photo_queries=_normalize_image_queries(
                    draft_json.get("image_queries")
                ),
            )
        except Exception as exc:  # noqa: BLE001 - fallback teks, jangan gagal
            print(f"[word_injector] Subdoc pengetahuan gagal ({exc}) - pakai Listing")
            context["pengetahuan_content"] = Listing(content)

    gambar = str(draft_json.get("lik_gambar_kerja") or "").strip()
    if gambar and "```" in gambar:
        # Ronde 12: gambar kerja di-scan blok mermaid pertama lalu diskalakan
        # pas kotak 120x100mm (dulu Mm(150) penuh -> 150x594mm, dikritik
        # reviewer "terlalu besar, tolong dikecilkan dan disesuaikan").
        m = _MERMAID_RE.search(gambar)
        png = _render_mermaid_safe(
            m.group(1) if m else gambar, tmp_dir / "gambar_kerja.png"
        )
        if png is not None:
            width_mm = _fit_png(png, *_IMG_GAMBAR_KERJA_MAX) or 120.0
            context["lik_gambar_kerja"] = InlineImage(
                tpl, str(png), width=Mm(width_mm)
            )
        else:
            context["lik_gambar_kerja"] = Listing(gambar)
    elif gambar:
        context["lik_gambar_kerja"] = Listing(gambar) if "\n" in gambar else gambar

    # Selalu pastikan identitas modul terisi
    context["module_title"] = module.get("module_title", context.get("module_title", ""))
    if "module_id" in context:
        context["module_id"] = module.get("module_id", "")

    tpl.render(context)

    # --- Cover page (ronde 18) ---
    # Ronde 17 memakai FOTO ASLI template apa adanya (overlay foto internet
    # DITOLAK reviewer krn menutupi gambar template). Ronde 18 memenuhi
    # permintaan user "cover page ... generated with replicate API" tanpa
    # mengulang kesalahan itu: byte `word/media/image3.jpg` DI DALAM template
    # ditukar dgn ilustrasi Replicate - slot, posisi, ukuran (205.9x144.1 mm),
    # dan tata letak textbox judul tidak berubah sama sekali, jadi gambar
    # dijamin terlihat utk SEMUA teks (judul berada di atas gambar, bukan
    # menimpanya). _apply_generated_cover() mengembalikan False (cover
    # template dipertahankan) bila Replicate mati/token kosong.
    module_title = str(module.get("module_title") or context.get("module_title") or "")

    # --- Simpan hasil (atomik) ---
    filename = f'{module.get("module_id", "M?")}_{_slugify(module.get("module_title", "modul"))}.docx'
    out_path = out_dir / filename
    # Tulis ke file .tmp lalu os.replace -> render paralel antar user tidak
    # pernah meninggalkan .docx setengah jadi yang bisa terunduh korup.
    # Windows (dev): file target masih terbuka di Word -> Access denied;
    # fallback simpan langsung agar demo/tes lokal tetap jalan.
    tmp_path = out_path.with_suffix(".docx.tmp")
    tpl.save(str(tmp_path))
    # Normalisasi pasca-render (WS3b) DI DALAM .tmp dulu: numbering per baris,
    # prefiks ordinal dibuang, sisa warna biru -> hitam.
    _normalize_rendered_docx(tmp_path)
    # Tukar cover DI DALAM .tmp dulu (bukan di out_path) supaya pembaca
    # tidak pernah melihat zip setengah tertulis; baru lalu dipublikasikan.
    _apply_generated_cover(tmp_path, draft_json, module_title)
    try:
        os.replace(str(tmp_path), str(out_path))
    except PermissionError:
        os.unlink(str(tmp_path))
        tpl.save(str(out_path))
        _normalize_rendered_docx(out_path)
        _apply_generated_cover(out_path, draft_json, module_title)
        print(f"[word_injector] Replace atomik terblokir (file terbuka?) - simpan langsung: {out_path}")
    print(f"[word_injector] Saved: {out_path}")
    return out_path


def inject_all_modules(state: GlobalState, output_dir: Path = None) -> List[Path]:
    """Loop semua modul yang sudah PASS lalu rakit .docx satu per satu.

    Modul yang gagal render tidak menghentikan modul lain (error dicatat,
    proses lanjut) supaya satu modul bermasalah tidak membatalkan batch.
    """
    results: List[Path] = []
    for module in state.get("modules") or []:
        try:
            results.append(inject_module(module, output_dir))
        except Exception as exc:  # noqa: BLE001 - jangan gagalkan batch
            print(f"[word_injector] GAGAL modul {module.get('module_id')}: {exc}")
    return results


# ----------------------------------------------------------------------
# Demo/contoh penggunaan: python tools/word_injector.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    demo_pengetahuan = (
        "1. Persiapan\n"
        "Perencanaan dilakukan untuk memastikan seluruh kebutuhan tersedia "
        "sebelum pekerjaan dimulai. Instruktur menjelaskan urutan kerja.\n"
        "1.1 Jenis alat\n"
        "Perencanaan adalah proses menentukan tujuan dan langkah kerja.\n"
        "```mermaid\n"
        "flowchart TD\n"
        "A[Mulai] --> B[Menyiapkan alat]\n"
        "B --> C[Cek kelengkapan]\n"
        "C --> D[Melaksanakan pekerjaan]\n"
        "D --> E[Membuat laporan]\n"
        "```\n"
        "2. langkah list bernomor tidak jadi heading\n"
        "Sarana dan prasarana dicek kelengkapannya sebelum digunakan."
    )
    demo_module = ModuleState(
        module_id="DEMO",
        module_title="Mengoperasikan Kondenser",
        syllabus_content="| Elemen Kompetensi | KUK | Alokasi Waktu |\n|---|---|---|\n| Persiapan | Alat lengkap | 1 JP |",
        syllabus_rows=[
            {"elemen_no": "1.", "elemen": "Persiapan", "kuk_no": "1.1",
             "kuk": "Alat lengkap", "indikator": "Alat teridentifikasi",
             "pengetahuan": "Penjelasan tentang:\n1. Jenis alat\n2. Fungsi alat",
             "keterampilan": "Mengidentifikasi alat", "durasi": "1 JP"},
        ],
        draft_json={
            "pengetahuan_content": demo_pengetahuan,
            "lik_gambar_kerja": (
                "```mermaid\nflowchart TD\nA[Mulai] --> B[Cek alat]\nB --> C[Kerjakan]\n```"
            ),
            "cover_image_query": "steam power plant condenser",
            "image_queries": [
                {"subbab": "1", "query": "industrial hand tools set",
                 "judul": "Jenis-jenis alat tukang industri"},
            ],
            "elemen_rows": [
                {"elemen_no": "1.", "elemen": "Persiapan", "kuk_no": "1.1",
                 "kuk": "Alat lengkap", "indikator": "Alat teridentifikasi",
                 "pengetahuan": "1. Jenis alat\n2. Fungsi alat",
                 "keterampilan": "Mengidentifikasi alat", "durasi": "1 JP"},
            ],
            "bahan_rows": [{"no": "1", "nama": "Kertas HVS", "spek": "A4 80gsm", "jumlah": "10 lembar"}],
            # Ronde 18 (WS3b): daftar bernomor + prefiks ordinal manual -
            # latihan untuk normalisasi pasca-render (numbering per baris +
            # prefiks ordinal dibuang).
            "lik_peralatan": "Helm keselamatan\nSepatu keselamatan\nSarung tangan",
            "lik_langkah_kerja": "1. Menyiapkan alat kerja\n2. Melaksanakan pekerjaan",
            "evaluasi_pengetahuan": ("1) Pengetahuan tentang jenis alat\n"
                                     "2) Pengetahuan tentang fungsi alat"),
            "evaluasi_praktik": "1) Mengidentifikasi alat",
            "kamus_rows": [{"no": "a.", "istilah": "Kondenser", "arti": "Komponen pendingin"}],
            "referensi_rows": [{"no": "a.", "url": "https://kbbi.co.id"}],
        },
        evaluator_feedback="",
        status_evaluasi="PASS",
        iteration_count=1,
    )
    print(f"Tag template ditemukan: {get_template_tags()}")
    path = inject_module(demo_module, output_dir=OUTPUT_DIR / "demo")
    print(f"Demo render sukses -> {path}")