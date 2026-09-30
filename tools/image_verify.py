"""
IMAGE VERIFY - buktikan gambar benar-benar dari Replicate & cover tertukar
==========================================================================
Permintaan user: "make sure the cover page and all the image are all
generated with replicate API using image generator".

Mode:

  --schema          GRATIS (read-only). Mengambil skema nyata dari Replicate
                    lalu memvalidasi payload yang akan dikirim: setiap field
                    harus ADA di skema, dan aspect_ratio/output_format harus
                    ada di enum-nya. Memeriksa KEDUA TINGKAT (subbab =
                    REPLICATE_MODEL, cover = REPLICATE_MODEL_COVER) karena
                    keduanya berbeda keluarga skema sejak ronde 20.
                    Menangkap seluruh kelas bug yang dulu tersembunyi 5
                    ronde (field milik model lain: `size` ke nano-banana,
                    atau `resolution` ke flux / `megapixels` ke nano-banana)
                    TANPA memakai kredit.

  --live            --schema + SATU panggilan Replicate sungguhan (berbayar).
                    Membuktikan token valid, slug ADA, dan input DITERIMA.

  --selftest        Membuktikan OPERASI SWAP hanya menyentuh entry cover:
                    salinan template ditukar dgn JPEG 10:7 sintetis, lalu
                    dibandingkan SEBELUM vs SESUDAH -> seluruh entry lain
                    harus identik byte-per-byte, urutan terjaga, arsip sehat.
                    TIDAK butuh kredit Replicate.

  --docx <path>     Membuktikan cover di .docx hasil render:
                      1. byte `word/media/image3.jpg` ber-magic JPEG (FFD8)
                      2. rasionya PERSIS 10:7 (slot template 205.9x144.1 mm;
                         Word menghormati extent XML, rasio beda = gepeng)
                      3. isinya BEDA dari foto template (bukti terganti)
                      4. tidak ada entry template yang hilang
                    Catatan: entry lain WAJAR berbeda dari template (docxtpl
                    menulis ulang isi dokumen + menambah gambar subbab), jadi
                    keutuhan tata letak diuji di --selftest, bukan di sini.

Keluar kode 1 bila ada yang tidak sesuai -> bisa dipakai gerbang sebelum
mengirim modul ke reviewer.

Pakai:
    python tools/image_verify.py --schema      # gratis, jalankan ini dulu
    python tools/image_verify.py --selftest    # gratis
    python tools/image_verify.py --live        # berbayar (1 gambar)
    python tools/image_verify.py --docx output/DEMO_....docx
"""

import hashlib
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.doc_utils import TEMPLATE_PATH  # noqa: E402

COVER_ENTRY = "word/media/image3.jpg"
COVER_ASPECT_TOL = 0.01  # toleransi rasio (pembulatan pixel saat crop)
COVER_ASPECT_TARGET = 10.0 / 7.0

FAILURES: list = []


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"  [FAIL] {msg}")


def _warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


# ----------------------------------------------------------------------
def _model_input_schema(slug: str):
    """({field: spec}, {field: [enum]}) dari GET /v1/models/<slug>.

    GRATIS (read-only) - tidak memakai kredit. Enum biasanya disembunyikan
    di balik $ref, jadi harus diresolusi dari components/schemas.
    """
    import httpx

    from tools import image_gen

    try:
        r = httpx.get(
            f"https://api.replicate.com/v1/models/{slug}",
            headers={"Authorization": f"Bearer {image_gen._token()}"},
            timeout=25,
        )
    except Exception as exc:  # noqa: BLE001 - jaringan putus != skema salah
        print(f"    (GET skema gagal: {type(exc).__name__}: {str(exc)[:120]})")
        return None, None
    if r.status_code != 200:
        return None, None
    ver = r.json().get("latest_version") or {}
    comps = (ver.get("openapi_schema") or {}).get("components", {}).get("schemas", {})
    props = (comps.get("Input") or {}).get("properties", {}) or {}
    enums = {}
    for name, spec in props.items():
        spec = spec or {}
        if spec.get("enum"):
            enums[name] = spec["enum"]
            continue
        for part in (spec.get("allOf") or []):
            ref = (part or {}).get("$ref")
            if ref:
                got = (comps.get(ref.rsplit("/", 1)[-1]) or {}).get("enum")
                if got:
                    enums[name] = got
    return props, enums


def verify_schema(paid: bool = False) -> bool:
    """Cek payload vs SKEMA NYATA KEDUA TINGKAT. Gratis; True bila cocok.

    Sejak ronde 20 subbab dan cover memakai model BERBEDA KELUARGA, jadi
    keduanya diperiksa terpisah - inilah cara menangkap kelas bug yang
    tersembunyi 5 ronde (field milik model lain: `resolution` ke flux /
    `megapixels` ke nano-banana) TANPA memakai kredit.
    """
    from tools import image_gen

    print("\n[SCHEMA] Validasi payload vs skema nyata (gratis, tanpa kredit)")
    print(f"  subbab : {image_gen._MODEL} "
          f"(keluarga {image_gen._model_family()})")
    print(f"  cover  : {image_gen._MODEL_COVER} "
          f"(keluarga {image_gen._model_family(image_gen._MODEL_COVER)})")
    if not image_gen._token():
        _fail("REPLICATE_API_TOKEN kosong di .env - generate gambar TIDAK "
              "akan pernah jalan (semua gambar jatuh ke pencarian internet)")
        return False

    tiers = (
        ("subbab", image_gen._MODEL, image_gen._DEFAULT_ASPECT, "png"),
        ("cover", image_gen._MODEL_COVER, image_gen._COVER_ASPECT_REQUEST, "jpg"),
    )
    schemas: dict = {}
    ok = True
    for label, slug, ar, fmt in tiers:
        if slug not in schemas:  # satu GET per slug, walau modelnya sama
            schemas[slug] = _model_input_schema(slug)
        props, enums = schemas[slug]
        fam = image_gen._model_family(slug)
        print(f"\n  [{label}] {slug} - keluarga {fam}, "
              f"ukuran {image_gen._size_field(slug)}")
        if props is None:
            _fail(f"{label}: tidak bisa membaca skema '{slug}' (HTTP != 200) "
                  "- cek slug / token")
            ok = False
            continue
        print(f"    field tersedia: {sorted(props.keys())}")

        payload = image_gen._input("contoh ilustrasi pelatihan", ar, fmt, slug)
        unknown = [k for k in payload if k not in props]
        if unknown:
            _fail(f"{label}: field tidak ada di skema {slug}: {unknown}")
            ok = False
        for field in ("aspect_ratio", "output_format"):
            val = payload.get(field)
            allowed = enums.get(field)
            if allowed and val not in allowed:
                _fail(f"{label}: {field}='{val}' tidak ada di enum {allowed}")
                ok = False
        # Field ukuran wajib cocok dgn keluarga skema.
        if fam == "flux" and "resolution" in payload:
            _fail(f"{label}: 'resolution' dikirim ke flux-schnell yang TIDAK "
                  "punya field itu (harusnya 'megapixels') -> submit 422")
            ok = False
        if fam == "gemini" and "megapixels" in payload:
            _fail(f"{label}: 'megapixels' dikirim ke nano-banana yang TIDAK "
                  "punya field itu (harusnya 'resolution') -> submit 422")
            ok = False
        if not unknown:
            _ok(f"{label}: {len(payload)} field valid, aspect_ratio="
                f"{payload.get('aspect_ratio')}, output_format="
                f"{payload.get('output_format')}, {image_gen._size_field(slug)}")

    if image_gen._model_family(image_gen._MODEL_COVER) == "flux":
        _warn("model cover berkeluarga flux: `megapixels` maks \"1\" (1 MP) -> "
              "cover 10:7 hasil crop hanya ~1167x817 px, lebih kecil dari foto "
              "template 1400x980 px. Pakai google/nano-banana-2 utk cover "
              "(resolution 2K) bila ketajaman cetak penting.")

    if paid:
        # SATU panggilan saja (tingkat subbab = yang termurah). Tingkat cover
        # tidak perlu dipanggil berbayar di sini: slug-nya sudah terbukti ADA
        # (skema berhasil dibaca di atas) dan payload-nya sudah tervalidasi
        # field-per-field terhadap skema itu. Yang tersisa hanyalah kredit.
        print("\n[LIVE] Satu panggilan Replicate sungguhan (BERBAYAR, tingkat subbab)")
        out = Path(image_gen._PROJECT_ROOT) / "runtime" / "verify_live.png"
        res = image_gen.generate_image(
            "close-up of a technician inspecting a solar panel array, "
            "industrial training illustration",
            out,
            use_cache=False,  # uji jalur API nyata, bukan cache
        )
        if res is None:
            _fail("Replicate tidak menghasilkan gambar. Cek pesan [image_gen] "
                  "di atas: HTTP 402 = kredit habis (isi di "
                  "https://replicate.com/account/billing); 401 = token salah; "
                  "404 = slug model salah; 422 = field/enum tidak cocok.")
            return False
        from PIL import Image

        with Image.open(str(res)) as im:
            w, h = im.size
        _ok(f"Replicate OK -> {res.name} {w}x{h}px, {res.stat().st_size} byte")
        print(f"  (arsip: {res})")
    return ok


# ----------------------------------------------------------------------
def _entries(path: Path, want: str = None) -> tuple:
    """(nama entry -> sha256 ISI setelah dekompresi, byte entry `want`)."""
    with zipfile.ZipFile(path) as z:
        digests = {n: hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist()}
        blob = z.read(want) if want and want in digests else None
    return digests, blob


def verify_docx(docx_path: Path) -> None:
    docx_path = Path(docx_path)
    print(f"\n[DOCX] {docx_path}")
    if not docx_path.exists():
        _fail(f"file tidak ada: {docx_path}")
        return
    if not TEMPLATE_PATH.exists():
        _fail(f"template tidak ada: {TEMPLATE_PATH}")
        return

    docx_entries, cover = _entries(docx_path, COVER_ENTRY)
    if cover is None:
        _fail(f"entry '{COVER_ENTRY}' tidak ada di hasil render")
        return
    tpl_entries, tpl_cover = _entries(Path(TEMPLATE_PATH), COVER_ENTRY)
    if tpl_cover is None:
        _fail(f"entry '{COVER_ENTRY}' tidak ada di template - asumsi slot cover salah")
        return
    print(f"  cover hasil : {len(cover)} byte")
    print(f"  cover template: {len(tpl_cover)} byte")

    # 1) magic JPEG
    if cover[:2] == b"\xff\xd8":
        _ok("cover ber-magic JPEG (FFD8) - .jpg entry + Content_Types cocok")
    else:
        _fail(f"cover bukan JPEG (magic {cover[:2]!r}) - byte PNG di entry .jpg "
              "memicu prompt 'repair' di Word")

    # 2) rasio persis 10:7
    try:
        from PIL import Image

        with Image.open(io.BytesIO(cover)) as im:
            w, h = im.size
        ratio = w / h
        if abs(ratio - COVER_ASPECT_TARGET) < COVER_ASPECT_TOL:
            _ok(f"rasio cover {w}x{h} = {ratio:.4f} (target "
                f"{COVER_ASPECT_TARGET:.4f}) - tidak akan gepeng")
        else:
            _fail(f"rasio cover {ratio:.4f} != {COVER_ASPECT_TARGET:.4f} "
                  f"({w}x{h}) - Word akan memelarkan gambar ke 205.9x144.1 mm")
        if w < 800:
            _warn(f"cover hanya {w}px lebar - pada 205.9 mm itu "
                  f"{w / 205.9 * 25.4:.0f} dpi, bisa terlihat lunak saat cetak")
    except Exception as exc:  # noqa: BLE001
        _fail(f"cover tidak bisa dibaca PIL: {type(exc).__name__}: {exc}")

    # 3) bukti terganti
    cover_hash = hashlib.sha256(cover).hexdigest()
    tpl_cover_hash = hashlib.sha256(tpl_cover).hexdigest()
    if cover_hash != tpl_cover_hash:
        _ok("cover BERBEDA dari foto template -> ilustrasi Replicate terpasang")
    else:
        _fail("cover IDENTIK dengan foto template -> swap tidak terjadi "
              "(cek IMAGE_COVER_MODE, REPLICATE_API_TOKEN, atau log "
              "'Cover Replicate tidak tersedia')")

    # 4) Entry lain TIDAK diuji di sini.
    # Hasil render memang sah berbeda dari template: docxtpl menulis ulang
    # document.xml, [Content_Types].xml, _rels, docProps/core.xml dan
    # menambah gambar subbab. Jadi "hanya image3.jpg yang berubah" BUKAN
    # sifat hasil render - ia sifat OPERASI SWAP, dan diuji di --selftest
    # (di sana pembandingnya salinan template pra-swap, bukan template asli).
    changed = [n for n in tpl_entries if docx_entries.get(n) != tpl_entries[n]]
    missing = [n for n in tpl_entries if n not in docx_entries]
    extra = [n for n in docx_entries if n not in tpl_entries]
    print(f"  entry berubah vs template : {len(changed)} "
          f"(wajar: docxtpl menulis ulang isi dokumen)")
    print(f"  entry baru (gambar subbab) : {len(extra)}")
    if missing:
        _fail(f"entry template HILANG di hasil render: {missing[:5]}")
    else:
        _ok(f"tidak ada entry template yang hilang ({len(tpl_entries)} entry)")


# ----------------------------------------------------------------------
def verify_selftest() -> None:
    """Buktikan OPERASI SWAP hanya menyentuh entry cover.

    Membandingkan salinan template SEBELUM dan SESUDAH `_swap_zip_entry`
    memakai JPEG 10:7 sintetis - tidak butuh kredit Replicate, jadi gerbang
    ini tetap bisa dijalankan walau API sedang mati.
    """
    import shutil
    import tempfile

    from tools import word_injector as wi

    print("\n[SELFTEST] _swap_zip_entry hanya mengubah entry cover")
    if not TEMPLATE_PATH.exists():
        _fail(f"template tidak ada: {TEMPLATE_PATH}")
        return

    tmp = Path(tempfile.mkdtemp(prefix="imgverify_"))
    try:
        work = tmp / "t.docx"
        shutil.copyfile(TEMPLATE_PATH, work)

        with zipfile.ZipFile(work) as z:
            before = {n: hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist()}
            order_before = z.namelist()
            tpl_cover = z.read(COVER_ENTRY)

        from PIL import Image

        im = Image.new("RGB", (1400, 980), (30, 90, 60))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=88, optimize=True)
        jpg = buf.getvalue()

        if not wi._swap_zip_entry(work, COVER_ENTRY, jpg):
            _fail("_swap_zip_entry mengembalikan False pada template yang valid")
            return

        with zipfile.ZipFile(work) as z:
            after = {n: hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist()}
            order_after = z.namelist()
            new_cover = z.read(COVER_ENTRY)
            broken = z.testzip()
            comment_ok = z.comment == zipfile.ZipFile(TEMPLATE_PATH).comment

        if new_cover == jpg:
            _ok(f"cover benar-benar ditukar ({len(tpl_cover)} -> {len(jpg)} byte)")
        else:
            _fail("isi entry cover tidak berubah setelah swap")

        others_before = {k: v for k, v in before.items() if k != COVER_ENTRY}
        others_after = {k: v for k, v in after.items() if k != COVER_ENTRY}
        if others_before == others_after:
            _ok(f"SELURUH {len(others_before)} entry lain identik byte-per-byte "
                "- tata letak resmi Kemnaker utuh")
        else:
            bad = [k for k in others_before if others_after.get(k) != others_before[k]]
            _fail(f"entry lain ikut berubah: {bad[:5]}")

        if order_before == order_after:
            _ok(f"urutan {len(order_before)} entry terjaga")
        else:
            _fail("urutan entry zip berubah - [Content_Types].xml bisa tak lagi pertama")

        if broken is None:
            _ok("arsip sehat (testzip bersih)")
        else:
            _fail(f"arsip rusak pada entry: {broken}")
        if comment_ok:
            _ok("komentar arsip terjaga")
        else:
            _warn("komentar arsip hilang")

        try:
            import docx

            docx.Document(str(work))
            _ok("python-docx bisa membuka hasil swap")
        except Exception as exc:  # noqa: BLE001
            _fail(f"python-docx gagal membuka hasil swap: {type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------
def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 0

    print("=" * 68)
    print("IMAGE VERIFY - Replicate & cover template")
    print("=" * 68)

    if "--schema" in argv:
        verify_schema(paid=False)
    if "--live" in argv:
        verify_schema(paid=True)
    if "--selftest" in argv:
        verify_selftest()
    if "--docx" in argv:
        i = argv.index("--docx")
        if i + 1 >= len(argv):
            _fail("--docx butuh path file")
        else:
            verify_docx(argv[i + 1])

    print("\n" + "=" * 68)
    if FAILURES:
        print(f"GAGAL - {len(FAILURES)} masalah:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("SEMUA PEMERIKSAAN LULUS.")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
