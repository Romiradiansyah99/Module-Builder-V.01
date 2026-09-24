"""
IMAGE GEN - Generate gambar via Replicate (ronde 13b)
=====================================================
Permintaan user: "untuk beberapa flow yang perlu generate gambar bisa
pake API replicate ku" - jadi Agent 2 boleh memilih mode "generate" pada
image_queries/cover (ilustrasi proses/abstrak yang memang tidak ada
fotonya), dan tools ini mengeksekusinya.

Model: google/nano-banana-v2 (Gemini 2.5 Flash Image - "Nano Banana 2") via
Replicate (/v1/models/<owner>/<name>/predictions). Lebih lambat & lebih mahal
dari flux-schnell (±15-60s) tapi kualitas generatif jauh lebih tinggi.

Token dari env REPLICATE_API_TOKEN (jangan pernah dicetak/log). Semua
kegagalan aman: return None - pemanggil (word_injector) jatuh ke pencari
gambar internet, injeksi Word tidak boleh gagal karena generate.
"""

import os
import re
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

_API_BASE = "https://api.replicate.com/v1"
_MODEL = "google/nano-banana-v2"
_POLL_TIMEOUT_S = 180

# Ronde 15 (keluhan user: "generate gambar ny kenapa jelek"): prompt polos
# pada flux-schnell menghasilkan foto-palsu berkualitas rendah. Teruji
# side-by-side: menambahkan gaya flat vector illustration menghasilkan
# ilustrasi profesional yang cocok untuk modul pelatihan (jauh lebih baik,
# biaya sama). Model lebih mahal (flux-dev) tidak lebih baik utk gaya ini.
# NEGASI DILARANG: "no text/no watermark" justru MEMICU teks besar di
# gambar (terukur: "WASTE" ditulis di papan); "wordless artwork" teruji
# bersih 3/3.
_STYLE_RE = re.compile(r"illustration|vector|diagram|icon|drawing|sketch|logo", re.I)
_STYLE_SUFFIX = (
    ", clean flat vector illustration style, soft colors, simple shapes, "
    "professional educational illustration for a training module, wordless artwork"
)


def _styled_prompt(prompt: str) -> str:
    """Bungkus prompt user dgn gaya ilustrasi modul. Query yang sudah
    menyebut gaya sendiri tidak diberi prefiks ganda."""
    q = prompt.strip().rstrip(".")
    if not _STYLE_RE.search(q):
        q = "flat vector illustration of " + q
    return q + _STYLE_SUFFIX


# Nano Banana (Gemini 2.5 Flash Image) memakai kolom `size` (piksel "WxH"),
# BUKAN `aspect_ratio` seperti flux-schnell. 1152x864 = 4:3 landscape.
_INPUT_SIZE = "1152x864"


def _input(prompt: str):
    return {"prompt": _styled_prompt(prompt), "size": _INPUT_SIZE}


def _token() -> str:
    return os.getenv("REPLICATE_API_TOKEN", "").strip()


def generate_image(prompt: str, out_path: Path) -> Optional[Path]:
    """Prompt teks -> PNG di out_path. None bila token kosong / API gagal /
    output bukan gambar layak (min 200px)."""
    token = _token()
    if not token or not prompt or not prompt.strip():
        return None
    import httpx

    headers = {"Authorization": f"Bearer {token}"}
    try:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        r = httpx.post(
            f"{_API_BASE}/models/{_MODEL}/predictions",
            headers=headers,
            json={"input": _input(prompt)},
            timeout=30,
        )
        if r.status_code != 201:
            print(f"[image_gen] Replicate submit gagal (HTTP {r.status_code})")
            return None
        pred = r.json()
        # Polling: flux-schnell biasanya selesai 2-5 detik
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
                    r2 = httpx.post(
                        f"{_API_BASE}/models/{_MODEL}/predictions",
                        headers=headers,
                        json={"input": _input(prompt)}, timeout=30,
                    )
                    if r2.status_code == 201:
                        pred = r2.json()
                        continue
                break
            if pred.get("status") in ("failed", "canceled"):
                print(f"[image_gen] Replicate {pred.get('status')} - prompt dilewati")
                return None
            time.sleep(1.5)
            gid = pred.get("id")
            if not gid:
                print("[image_gen] prediction tanpa id - lewati")
                return None
            # Gateway Replicate kadang 502/503 TRANSIEN (terukur: 1 dari 3
            # poll) - retry pendek dalam batas deadline, jangan langsung
            # menyerah pada satu kegagalan jaringan.
            pred_ok = None
            for retry in range(3):
                try:
                    rr = httpx.get(f"{_API_BASE}/predictions/{gid}",
                                   headers=headers, timeout=20)
                    if rr.status_code == 200:
                        pred_ok = rr.json()
                        break
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(2.0)
            if pred_ok is None:
                print("[image_gen] Poll gagal 3x (gateway transien) - lewati")
                return None
            pred = pred_ok
        if not urls:
            # flux-schnell kadang sukses tanpa output - beri tahu alasannya,
            # jangan gagal senyap (dulu menyulitkan diagnosis).
            print(f"[image_gen] Timeout/kosong: status={pred.get('status')} "
                  f"error={str(pred.get('error'))[:120]} - lewati")
            return None
        img_url = urls[0] if isinstance(urls, list) else urls
        rd = httpx.get(img_url, timeout=30, follow_redirects=True)
        ct = rd.headers.get("content-type", "")
        if rd.status_code != 200 or "image" not in ct or len(rd.content) < 5000:
            print(f"[image_gen] Unduh output gagal (HTTP {rd.status_code}, "
                  f"ct={ct}, {len(rd.content)}B) - lewati")
            return None
        from PIL import Image

        import io

        im = Image.open(io.BytesIO(rd.content))
        im.load()
        if im.width < 200 or im.height < 200:
            print(f"[image_gen] Output terlalu kecil ({im.width}x{im.height}) - lewati")
            return None
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.save(str(out_path), format="PNG")
        print(f"[image_gen] Replicate OK ({im.width}x{im.height}px): "
              f"{prompt.strip()[:50]}")
        return out_path
    except Exception as exc:  # noqa: BLE001 - jangan gagalkan injeksi
        print(f"[image_gen] Replicate gagal ({type(exc).__name__}) - lewati")
        return None


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    demo = generate_image(
        "illustration of a surface condenser in a steam power plant, "
        "clean technical illustration, no text",
        _PROJECT_ROOT / "runtime" / "test_imggen.png",
    )
    print("hasil:", demo)