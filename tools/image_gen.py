"""
IMAGE GEN - Generate gambar via Replicate (ronde 18)
====================================================
Permintaan user: "make sure the cover page and all the image are all
generated with replicate API using image generator".

BUG YANG DIPERBAIKI DI RONDE 18 (root cause "gambar tidak AI-generated"):
  1. `_MODEL` dulu "google/nano-banana-v2" -> Replicate membalas **HTTP 404
     (model tidak ada)**, sehingga SETIAP generate gagal dan injeksi Word
     diam-diam jatuh ke pencari gambar internet (tools/image_search.py).
  2. Input dulu mengirim {"size": "1152x864"} - field `size` TIDAK ADA di
     skema model ini; field yang benar `aspect_ratio` + `resolution`.
  Keduanya sekarang diperbaiki dan diverifikasi: `tools/image_verify.py --live`.

Model default (ronde 19): **black-forest-labs/flux-schnell** - dipilih user
karena ~11x lebih murah dari nano-banana-2 (~$0.003 vs ~$0.039 per gambar).
Alasannya juga cocok: gaya gambar di sini menuntut "wordless artwork"
(ronde 15: teks yang muncul di gambar justru jadi masalah), jadi keunggulan
utama nano-banana - rendering teks yang akurat - memang sengaja dimatikan.

DUA TINGKAT (ronde 20) - `REPLICATE_MODEL` (subbab) + `REPLICATE_MODEL_COVER`:
  subbab : flux-schnell, dipanggil BANYAK kali per modul -> yang murah.
  cover  : google/nano-banana-2, dipanggil SEKALI per modul -> yang mahal,
           karena satu-satunya kelemahan flux di sini adalah UKURAN:
           `megapixels` mentok di "1" (maks 1 MP), sehingga cover 10:7 hasil
           crop hanya ~1167x817 px - LEBIH KECIL dari foto template yang
           digantikannya (1400x980 px = 173 dpi pada lebar 205.9 mm).
           nano-banana menerima `resolution` 1K/2K/4K, jadi cover bisa
           setara atau lebih tajam dari template.
Konsekuensinya kedua model BERBEDA KELUARGA SKEMA, jadi `_model_family()`
tidak boleh lagi membaca variabel global: setiap fungsi menerima `model=`.
Tanpa itu cover akan mengirim payload bentuk flux (`megapixels`,
`num_outputs`, `go_fast`) ke nano-banana yang tidak punya field itu -> 422
di SETIAP cover, persis kelas bug senyap yang tersembunyi 5 ronde.

SKEMA INPUT BERBEDA PER MODEL - INI SUMBER BUG SENYAP:
  flux-schnell : prompt, aspect_ratio ("1:1","16:9","21:9","3:2","2:3",
                 "4:5","5:4","3:4","4:3","9:16","9:21"), megapixels
                 ("1"|"0.25"), output_format (webp|jpg|png), output_quality,
                 num_outputs, go_fast, num_inference_steps, seed.
                 >>> TIDAK ADA field `resolution` <<<
  nano-banana-2: prompt, image_input, aspect_ratio (default
                 match_input_image), resolution (1K|2K|4K), google_search,
                 image_search, output_format (jpg|png).
                 >>> TIDAK ADA field `megapixels` <<<
Mengirim field milik model lain = submit 422. Karena itu _input() memilih
payload lewat _model_family(model), dan tools/image_verify.py memeriksa
kesesuaiannya untuk KEDUA model (`--schema`, gratis) lalu satu panggilan
nyata (`--live`, berbayar).

Token dari env REPLICATE_API_TOKEN (jangan pernah dicetak/log). Semua
kegagalan aman: return None - pemanggil (word_injector) jatuh ke pencari
gambar internet, injeksi Word tidak boleh gagal karena generate.
"""

import hashlib
import io
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

_API_BASE = "https://api.replicate.com/v1"
# Slug yang BENAR (diverifikasi HTTP 200). "google/nano-banana-v2" -> 404.
# Ronde 19: default pindah ke flux-schnell (lihat docstring modul).
# Ganti model cukup lewat env REPLICATE_MODEL - payload menyesuaikan sendiri
# via _model_family().
_MODEL = os.getenv("REPLICATE_MODEL", "black-forest-labs/flux-schnell").strip()
# Ronde 20 - TINGKAT KEDUA: model KHUSUS cover (dipanggil sekali per modul).
# Kosong -> default. Sama dgn REPLICATE_MODEL = perilaku satu model (ronde 19).
_MODEL_COVER = (
    os.getenv("REPLICATE_MODEL_COVER", "").strip() or "google/nano-banana-2"
)
_POLL_TIMEOUT_S = 180
# Paksa keluarga skema bila deteksi otomatis salah (model di luar yang
# dikenal): auto | flux | gemini | generic. "generic" hanya mengirim
# prompt+aspect_ratio+output_format (irisan teraman).
# Dua variabel karena default ronde 20 memang DUA keluarga berbeda: override
# global untuk model subbab, `_COVER` untuk model cover.
_FAMILY_OVERRIDE = (os.getenv("IMAGE_MODEL_FAMILY") or "auto").strip().lower()
_FAMILY_OVERRIDE_COVER = (
    os.getenv("IMAGE_MODEL_FAMILY_COVER") or "auto"
).strip().lower()
_FAMILIES = ("flux", "gemini", "generic")


def _detect_family(slug: str) -> str:
    """Tebak keluarga dari slug. "generic" bila tidak dikenal."""
    m = (slug or "").lower()
    if "flux" in m:
        return "flux"
    if "nano-banana" in m or "gemini" in m:
        return "gemini"
    return "generic"


def _model_family(model: str = None) -> str:
    """Keluarga skema input untuk `model` (None = model subbab) -> bentuk payload.

    Override env dipakai HANYA bila slug-nya tak dikenal; slug yang dikenal
    selalu menang. Sejak ronde 20 subbab (flux) dan cover (nano-banana)
    berbeda keluarga, jadi satu override global justru bisa mengirim field
    milik keluarga lain ke model yang sudah jelas-jelas dikenali.
    """
    slug = (model or _MODEL).strip()
    hint = _FAMILY_OVERRIDE_COVER if slug == _MODEL_COVER else _FAMILY_OVERRIDE
    if slug and _detect_family(slug) == "generic" and hint in _FAMILIES:
        return hint
    return _detect_family(slug)

# Ronde 15: prompt polos menghasilkan foto-palsu berkualitas rendah; gaya
# flat vector illustration teruji jauh lebih baik untuk modul pelatihan.
# NEGASI DILARANG: "no text/no watermark" justru MEMICU teks besar di
# gambar (terukur: "WASTE" ditulis di papan); "wordless artwork" teruji
# bersih 3/3.
_STYLE_RE = re.compile(r"illustration|vector|diagram|icon|drawing|sketch|logo", re.I)
_STYLE_SUFFIX = (
    ", clean flat vector illustration style, soft colors, simple shapes, "
    "professional educational illustration for a training module, wordless artwork"
)

# Rasio default untuk gambar subbab (kotak 135x100 mm ~ 4:3) dan cover.
# Cover template memakai slot 10:7 (205.9 x 144.1 mm) - lihat fit_aspect().
# "4:3" dan "3:2" keduanya ADA di enum aspect_ratio flux-schnell.
_DEFAULT_ASPECT = os.getenv("IMAGE_ASPECT", "4:3").strip() or "4:3"
_COVER_ASPECT_REQUEST = "3:2"   # rasio enum terdekat DI ATAS 10:7
_COVER_ASPECT_TARGET = 10.0 / 7.0

# Ukuran output: NAMA FIELD BERBEDA PER KELUARGA MODEL. Dulu field `size`
# dikirim ke nano-banana yang tidak memilikinya -> submit gagal senyap.
_RESOLUTION = os.getenv("IMAGE_RESOLUTION", "1K").strip() or "1K"   # gemini
# Cover dapat resolusi SENDIRI: ini satu-satunya gambar besar (205.9 mm) dan
# dipanggil sekali per modul, jadi menaikkannya hampir gratis. 2K -> cover
# 10:7 ~2048x1434 px (> 1400x980 template). Kosong tetap berarti 2K.
_RESOLUTION_COVER = (
    os.getenv("IMAGE_COVER_RESOLUTION", "").strip() or "2K"
)
# flux-schnell: megapixels HANYA menerima "1" atau "0.25" (maks 1 MP, jadi
# cover 10:7 hasil crop ~1167x817 px - lebih kecil dari foto template
# 1400x980 px; justru INI alasan cover memakai model lain - lihat README).
_MEGAPIXELS = os.getenv("IMAGE_MEGAPIXELS", "1").strip() or "1"
# 0-100, hanya berpengaruh utk jpg/webp (png diabaikan).
_OUTPUT_QUALITY = int(os.getenv("IMAGE_OUTPUT_QUALITY", "90"))

# Format berkas gambar SUBBAB (cover selalu JPEG - lihat generate_cover).
# PNG = default/perilaku lama: lossless, tapi satu modul 21 gambar = ~10 MB.
# "jpg" (quality 88 + optimize) menurunkan dokumen ke ~2 MB tanpa beda
# kasat mata pada kotak template 135x100 mm - foto memang bahan JPEG, bukan
# diagram bergaris. HANYA png|jpg: webp ditolak python-docx saat add_picture.
# CATATAN BIAYA: format ikut jadi kunci cache, jadi mengganti nilai ini
# membuat cache lama tidak terpakai SEKALI (generate ulang = gambar berbayar).
_SUBBAB_FORMAT = (os.getenv("IMAGE_OUTPUT_FORMAT", "png") or "png").strip().lower()
if _SUBBAB_FORMAT in ("jpeg", "jpe"):
    _SUBBAB_FORMAT = "jpg"
if _SUBBAB_FORMAT not in ("png", "jpg"):
    _SUBBAB_FORMAT = "png"


def subbab_output_format() -> str:
    """Format berkas gambar subbab (IMAGE_OUTPUT_FORMAT, png|jpg)."""
    return _SUBBAB_FORMAT


# Percobaan ulang utk kegagalan JARINGAN transien (lihat `_net_retry`).
# 3 = dua kesempatan tambahan setelah percobaan pertama; jeda 2s lalu 4s.
_NET_TRIES = max(1, int(os.getenv("IMAGE_NET_RETRIES", "3") or 3))


def _resolution_for(model: str = None) -> str:
    """Nilai `resolution` (keluarga gemini) - cover punya setelan sendiri."""
    return _RESOLUTION_COVER if (model or _MODEL).strip() == _MODEL_COVER else _RESOLUTION


def _styled_prompt(prompt: str) -> str:
    """Bungkus prompt user dgn gaya ilustrasi modul. Query yang sudah
    menyebut gaya sendiri tidak diberi prefiks ganda."""
    q = prompt.strip().rstrip(".")
    if not _STYLE_RE.search(q):
        q = "flat vector illustration of " + q
    return q + _STYLE_SUFFIX


def _size_field(model: str = None) -> str:
    """Nama+nilai field ukuran milik keluarga `model` (None = subbab)."""
    fam = _model_family(model)
    if fam == "flux":
        return f"megapixels={_MEGAPIXELS}"
    if fam == "gemini":
        return f"resolution={_resolution_for(model)}"
    return "default"


def _input(prompt: str, aspect_ratio: str, output_format: str,
           model: str = None) -> dict:
    """Payload input SESUAI SKEMA `model` (None = subbab) - jangan campur
    field antar keluarga (mengirim `resolution` ke flux = 422, dan sebaliknya)."""
    payload = {
        "prompt": _styled_prompt(prompt),
        "aspect_ratio": aspect_ratio or _DEFAULT_ASPECT,
        "output_format": output_format,
    }
    fam = _model_family(model)
    if fam == "flux":
        payload.update({
            "megapixels": _MEGAPIXELS,
            "num_outputs": 1,
            "output_quality": _OUTPUT_QUALITY,
            # go_fast=True = fp8 quantized (default model, tercepat). Harga
            # per gambar SAMA; False -> bf16, lebih lambat tapi lebih baik.
            "go_fast": True,
        })
    elif fam == "gemini":
        payload["resolution"] = _resolution_for(model)
    return payload


def _token() -> str:
    return os.getenv("REPLICATE_API_TOKEN", "").strip()


# ----------------------------------------------------------------------
# Cache hasil generate: prompt yang sama tidak dibayar dua kali (injeksi
# Word dijalankan ulang tiap approve/regenerate modul).
# ----------------------------------------------------------------------
def cache_dir() -> Path:
    from rag import config

    return config.IMAGE_CACHE_DIR


def _cache_key(prompt: str, aspect_ratio: str, output_format: str,
               model: str = None) -> str:
    # SLUG MODEL + `_size_field()` ikut jadi kunci -> ganti model atau ukuran
    # TIDAK menyajikan gambar lama dari cache (kalau tidak, hasil flux akan
    # dipakai ulang untuk cover setelah cover pindah ke nano-banana).
    raw = (f"{(model or _MODEL).strip()}|{prompt}|{aspect_ratio}|"
           f"{output_format}|{_size_field(model)}")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _from_cache(prompt: str, out_path: Path, aspect_ratio: str,
                output_format: str, model: str = None) -> Optional[Path]:
    src = cache_dir() / f"{_cache_key(prompt, aspect_ratio, output_format, model)}.{output_format}"
    if not src.exists() or src.stat().st_size < 5000:
        return None
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, out_path)
        print(f"[image_gen] cache hit: {prompt.strip()[:50]}")
        return out_path
    except Exception:  # noqa: BLE001 - cache rusak = anggap miss
        return None


def _to_cache(prompt: str, src: Path, aspect_ratio: str, output_format: str,
              model: str = None) -> None:
    try:
        dst = cache_dir() / f"{_cache_key(prompt, aspect_ratio, output_format, model)}.{output_format}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            shutil.copyfile(src, dst)
    except Exception:  # noqa: BLE001 - cache opsional, jangan gagalkan generate
        pass


# ----------------------------------------------------------------------
# Replicate
# ----------------------------------------------------------------------
def _net_retry(fn, label: str, attempts: int = None, backoff: float = 2.0):
    """Jalankan `fn` dengan retry HANYA utk kegagalan JARINGAN transien.

    Terukur (ronde 18, run verifikasi): satu `ConnectTimeout` saat handshake TLS
    membuat **cover** jatuh ke foto template, dan satu `ReadTimeout` membuat
    gambar subbab jatuh ke foto internet - keduanya senyap, padahal keputusan
    yang disepakati adalah cover & gambar = ilustrasi Replicate. Kegagalan itu
    sementara; tanpa retry, gangguan sekejap langsung menurunkan mutu dokumen.

    Return (nilai, exception). Exception hanya terisi bila SEMUA percobaan
    gagal. Kesalahan HTTP (4xx/5xx) TIDAK diulang di sini - itu bug payload/
    model, bukan jaringan; mengulangnya hanya membuang waktu dan kuota.
    """
    import httpx

    n = attempts or _NET_TRIES
    last = None
    for i in range(n):
        try:
            return fn(), None
        except (httpx.TransportError, OSError) as exc:
            last = exc
            if i + 1 < n:
                print(f"[image_gen] {label}: gangguan jaringan "
                      f"({type(exc).__name__}) - ulangi {i + 2}/{n}")
                time.sleep(backoff * (i + 1))
    return None, last


def _submit(prompt: str, aspect_ratio: str, output_format: str,
            model: str = None):
    """POST prediksi ke `model` (None = subbab). Return (pred_dict, err_text)."""
    import httpx

    slug = (model or _MODEL).strip()

    def _once():
        r = httpx.post(
            f"{_API_BASE}/models/{slug}/predictions",
            headers={"Authorization": f"Bearer {_token()}"},
            json={"input": _input(prompt, aspect_ratio, output_format, slug)},
            timeout=30,
        )
        if r.status_code != 201:
            # JANGAN buang badan error: 404 model / 422 field salah harus
            # terlihat di log. Inilah yang menyembunyikan bug ronde 13-17.
            return None, f"HTTP {r.status_code}: {r.text[:300]}"
        return r.json(), None

    pred, exc = _net_retry(_once, f"submit {slug.split('/')[-1]}")
    if exc is not None:
        return None, f"{type(exc).__name__}: {exc}"
    return pred


def _poll(pred) -> Optional[list]:
    """Polling sampai succeeded/failed/timeout. Return daftar URL output."""
    import httpx

    headers = {"Authorization": f"Bearer {_token()}"}
    deadline = time.time() + _POLL_TIMEOUT_S
    urls = None
    retried_empty = False  # terukur r15: kadang succeeded TANPA output
    while time.time() < deadline:
        if pred.get("status") == "succeeded":
            urls = pred.get("output")
            if not urls and not retried_empty:
                # succeeded tapi kosong -> submit ulang SEKALI; jangan
                # buang subbab hanya karena anomali gateway
                retried_empty = True
                time.sleep(2)
                again, _ = _submit(pred.get("_prompt", ""), pred.get("_aspect", _DEFAULT_ASPECT),
                                   pred.get("_fmt", "png"), pred.get("_model"))
                if again:
                    again["_prompt"] = pred.get("_prompt", "")
                    again["_aspect"] = pred.get("_aspect", _DEFAULT_ASPECT)
                    again["_fmt"] = pred.get("_fmt", "png")
                    # model ikut dibawa: tanpa ini retry cover akan memakai
                    # model subbab (payload beda keluarga -> 422)
                    again["_model"] = pred.get("_model")
                    pred = again
                    continue
            break
        if pred.get("status") in ("failed", "canceled"):
            print(f"[image_gen] Replicate {pred.get('status')} - prompt dilewati "
                  f"({str(pred.get('error'))[:120]})")
            return None
        time.sleep(1.5)
        gid = pred.get("id")
        if not gid:
            print("[image_gen] prediction tanpa id - lewati")
            return None
        # Gateway Replicate kadang 502/503 TRANSIEN (terukur: 1 dari 3 poll)
        # - retry pendek dalam batas deadline, jangan langsung menyerah.
        pred_ok = None
        for _ in range(3):
            try:
                rr = httpx.get(f"{_API_BASE}/predictions/{gid}", headers=headers, timeout=20)
                if rr.status_code == 200:
                    pred_ok = rr.json()
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2.0)
        if pred_ok is None:
            print("[image_gen] Poll gagal 3x (gateway transien) - lewati")
            return None
        pred_ok["_prompt"] = pred.get("_prompt", "")
        pred_ok["_aspect"] = pred.get("_aspect", _DEFAULT_ASPECT)
        pred_ok["_fmt"] = pred.get("_fmt", "png")
        pred_ok["_model"] = pred.get("_model")
        pred = pred_ok
    if not urls:
        print(f"[image_gen] Timeout/kosong: status={pred.get('status')} "
              f"error={str(pred.get('error'))[:120]} - lewati")
        return None
    return urls if isinstance(urls, list) else [urls]


def _download(img_url: str):
    """Unduh hasil -> PIL Image RGB. None bila bukan gambar layak."""
    import httpx

    rd, exc = _net_retry(
        lambda: httpx.get(img_url, timeout=30, follow_redirects=True), "unduh hasil")
    if rd is None:
        print(f"[image_gen] Unduh output gagal ({type(exc).__name__}: "
              f"{str(exc)[:120]}) - lewati")
        return None
    ct = rd.headers.get("content-type", "")
    if rd.status_code != 200 or "image" not in ct or len(rd.content) < 5000:
        print(f"[image_gen] Unduh output gagal (HTTP {rd.status_code}, "
              f"ct={ct}, {len(rd.content)}B) - lewati")
        return None
    from PIL import Image

    im = Image.open(io.BytesIO(rd.content))
    im.load()
    if im.width < 200 or im.height < 200:
        print(f"[image_gen] Output terlalu kecil ({im.width}x{im.height}) - lewati")
        return None
    return im.convert("RGB") if im.mode not in ("RGB", "L") else im


# ----------------------------------------------------------------------
# API publik
# ----------------------------------------------------------------------
def generate_image(prompt: str, out_path: Path, *,
                   aspect_ratio: str = None, output_format: str = "png",
                   use_cache: bool = True,
                   model: str = None) -> Optional[Path]:
    """Prompt teks -> gambar di out_path. None bila token kosong / API gagal.

    aspect_ratio: rasio enum model (mis. "4:3"). output_format: "png"|"jpg".
    model: slug Replicate; None = REPLICATE_MODEL (subbab). Cover memakai
    REPLICATE_MODEL_COVER lewat generate_cover(). Keluarga skema model INI
    yang menentukan payload, jadi jangan pernah mengirim payload model lain.
    Hasil disimpan apa adanya (PNG utk subbab, JPEG utk cover).
    """
    token = _token()
    if not token or not prompt or not prompt.strip():
        if not token:
            print("[image_gen] REPLICATE_API_TOKEN kosong - generate dilewati")
        return None
    slug = (model or _MODEL).strip()
    aspect_ratio = aspect_ratio or _DEFAULT_ASPECT
    out_path = Path(out_path)
    # Ekstensi berkas HARUS mencerminkan format isinya: nama `.png` berisi
    # byte JPEG membingungkan saat diperiksa manual (dan menyalahi pemetaan
    # .png -> image/png bila berkas itu masuk ke docx template).
    ok_suffixes = (".jpg", ".jpeg") if output_format == "jpg" else (".png",)
    if out_path.suffix.lower() not in ok_suffixes:
        out_path = out_path.with_suffix(ok_suffixes[0])
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if use_cache:
        hit = _from_cache(prompt, out_path, aspect_ratio, output_format, slug)
        if hit is not None:
            return hit

    try:
        pred, err = _submit(prompt, aspect_ratio, output_format, slug)
        if pred is None:
            print(f"[image_gen] Replicate submit gagal ({err})")
            return None
        pred["_prompt"] = prompt
        pred["_aspect"] = aspect_ratio
        pred["_fmt"] = output_format
        pred["_model"] = slug
        urls = _poll(pred)
        if not urls:
            return None
        im = _download(urls[0])
        if im is None:
            return None
        if output_format == "jpg":
            im.save(str(out_path), format="JPEG", quality=88, optimize=True)
        else:
            im.save(str(out_path), format="PNG")
        print(f"[image_gen] Replicate OK ({im.width}x{im.height}px, "
              f"{aspect_ratio}, {slug.split('/')[-1]}): {prompt.strip()[:50]}")
        if use_cache:
            _to_cache(prompt, out_path, aspect_ratio, output_format, slug)
        return out_path
    except Exception as exc:  # noqa: BLE001 - jangan gagalkan injeksi
        print(f"[image_gen] Replicate gagal ({type(exc).__name__}: {str(exc)[:120]}) - lewati")
        return None


def fit_aspect(im, target: float, bias_y: float = 0.65):
    """CROP gambar agar rasionya persis `target` (tanpa memelarkan/menambah
    bantalan - hanya memotong). Word menghormati extent XML di template,
    jadi rasio yang tidak pas = gambar gepeng.

    Bila terlalu tinggi: potong tinggi dgn bias ke bawah (bias_y=0.65) -
    subjek ilustrasi umumnya di pita bawah, sementara textbox judul cover
    ada di atas.
    """
    w, h = im.size
    if w <= 0 or h <= 0:
        return im
    cur = w / h
    if abs(cur - target) < 0.01:
        return im
    if cur > target:  # terlalu lebar -> potong kiri/kanan
        new_w = max(1, int(round(h * target)))
        x0 = max(0, (w - new_w) // 2)
        return im.crop((x0, 0, x0 + new_w, h))
    new_h = max(1, int(round(w / target)))  # terlalu tinggi -> potong atas/bawah
    y0 = max(0, min(int(round((h - new_h) * bias_y)), h - new_h))
    return im.crop((0, y0, w, y0 + new_h))


def generate_cover(query: str, out_path: Path) -> Optional[Path]:
    """Ilustrasi COVER pada rasio PERSIS 10:7 (slot template 205.9x144.1 mm).

    Memakai model TINGKAT KEDUA (`REPLICATE_MODEL_COVER`, default
    google/nano-banana-2) - bukan model subbab - karena flux-schnell tidak
    bisa melebihi 1 MP (`megapixels` maks "1") sehingga cover 10:7-nya hanya
    ~1167x817 px, lebih kecil dari foto template yang digantikan (1400x980).

    Meminta 3:2 (rasio enum terdekat di atas 10:7) lalu di-crop lokal ke
    10:7; tidak ada rasio enum yang bernilai 10:7. Disimpan sebagai JPEG
    karena entry yang diganti bernama `word/media/image3.jpg` dan
    [Content_Types].xml memetakan .jpg -> image/jpeg (byte PNG di sana
    memicu prompt "repair" Word).
    """
    jpg = generate_image(query, out_path, aspect_ratio=_COVER_ASPECT_REQUEST,
                         output_format="jpg", model=_MODEL_COVER)
    if jpg is None:
        return None
    try:
        from PIL import Image

        # Buka -> potong -> TUTUP file dulu, baru simpan ke path yang sama.
        # Di Windows menyimpan ke path yang masih terbuka = PermissionError.
        with Image.open(str(jpg)) as im:
            im.load()
            fitted = fit_aspect(im.convert("RGB"), _COVER_ASPECT_TARGET, bias_y=0.65)
            # batasi sisi panjang agar ukuran file mirip template (~139 KB)
            if fitted.width > 2500:
                ratio = 2500 / fitted.width
                fitted = fitted.resize(
                    (2500, max(1, int(round(fitted.height * ratio)))),
                    Image.LANCZOS,
                )
        fitted.save(str(jpg), format="JPEG", quality=88, optimize=True)
        print(f"[image_gen] Cover {fitted.width}x{fitted.height}px "
              f"(rasio {fitted.width / fitted.height:.3f})")
    except Exception as exc:  # noqa: BLE001 - tetap pakai gambar apa adanya
        print(f"[image_gen] Cover crop gagal ({type(exc).__name__}) - pakai apa adanya")
    return jpg


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    out = _PROJECT_ROOT / "runtime" / "test_imggen.png"
    demo = generate_image(
        "illustration of a surface condenser in a steam power plant, "
        "clean technical illustration",
        out,
    )
    print("subbab:", demo)
    cov = generate_cover(
        "cover illustration for a vocational training module on solar power plant "
        "maintenance, technicians working with solar panels and electrical equipment",
        _PROJECT_ROOT / "runtime" / "test_cover.jpg",
    )
    print("cover  :", cov)
