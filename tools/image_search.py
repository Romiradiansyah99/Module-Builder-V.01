"""
IMAGE SEARCH - Cari & unduh gambar relevan dari internet (ronde 13)
===================================================================
Permintaan user: cover page harus ada gambar yang RELEVAN dengan judul
modul, "ambil aja dari google" - jadi Agent 2 diberi "tool" pencari gambar:
Agent 2 menulis QUERY pencarian (cover_image_query + image_queries per
subbab), tool inilah yang mengeksekusinya saat injeksi Word.

Sumber (tanpa API key, dicoba berurutan):
1. DuckDuckGo Images (i.js, butuh token vqd) - hasil paling relevan,
   tapi dari IP datacenter sering 403 -> HANYA 1 percobaan cepat.
2. Wikimedia Commons API - sangat andal (foto teknis/industri bagus),
   lisensi bebas.

Semua kegagalan aman: return None, pemanggil memutuskan (skip gambar).
Tidak pernah raise - injeksi Word tidak boleh gagal karena internet.
"""

import io
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

import httpx

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Accept-Language": "id-ID,id;q=0.9,en;q=0.8"}

# Gambar yang terlalu kecil jelek di dokumen; terlalu besar bikin .docx
# puluhan MB - dinormalisasi saat unduh.
_MIN_PX = 300
_MAX_W_PX = 1400

# Ronde 15 (keluhan user: "gambar ga sesuai konteks"): pencarian yang
# dilonggarkan bertahap (ronde 13) kerap berakhir di judul tak berhubungan -
# terukur: subbab "memeriksa dokumen pengiriman sampah" dapat screenshot
# Excel & klipart generik karena hanya cocok kata "report/document".
# Gerbang relevansi: judul kandidat wajib memuat >=2 kata kunci query
# (atau semua bila kata kuncinya cuma 1), dan judul sampah ditolak.
_JUNK_TITLE_RE = re.compile(
    r"screenshot|excel|spreadsheet|libreoffice|microsoft office|ms office|"
    r"clip ?art|logo|icon\b|favicon|watermark|sample\b|template\b|"
    r"diagram\b|cartoon|comic|meme|screenshot of|chart\b|graph\b",
    re.IGNORECASE,
)
# Bukan foto: PDF arsip (Internet Archive), svg, djvu, video. Thumbnail-nya
# lolos ke URL thumb sehingga harus disaring eksplisit (terukur ronde 15:
# "Feeding garbage to hogs.pdf" hampir dipakai utk subbab timbangan sampah).
_BITMAP_EXT_RE = re.compile(r"\.(jpe?g|png|gif|webp|tiff?)$", re.IGNORECASE)
_STOPWORDS = {
    "the", "a", "an", "of", "at", "in", "on", "for", "with", "and", "or",
    "to", "by", "from", "into", "at", "di", "dan", "yang", "ke", "dari",
    "untuk", "dengan", "pada", "dalam",
}


def _keywords(query: str) -> List[str]:
    """Kata kunci bermakna dari query (bukan stopword, >=3 huruf)."""
    words = re.findall(r"[a-zA-Z]{3,}", query.lower())
    return [w for w in words if w not in _STOPWORDS]


def _is_junk_title(title: str) -> bool:
    t = title or ""
    if _JUNK_TITLE_RE.search(t):
        return True
    # Commons "File:<nama>.<ext>" - bukan gambar bitmap = bukan foto
    m = re.search(r"\.([a-z0-9]+)\s*$", t, re.IGNORECASE)
    return bool(m and not _BITMAP_EXT_RE.search(t))


def _relevance(cand: Dict, kws: List[str]) -> int:
    """Jumlah kata kunci query yang muncul di judul kandidat."""
    title = (cand.get("title") or "").lower()
    return sum(1 for w in kws if w in title)


def _rank_candidates(cands: List[Dict], kws: List[str]) -> List[Dict]:
    """Buang judul sampah, urutkan paling relevan dulu."""
    kws = kws or []
    alive = [c for c in cands if not _is_junk_title(c.get("title") or "")]
    if kws:
        alive.sort(key=lambda c: (_relevance(c, kws), c.get("source") == "duckduckgo"),
                   reverse=True)
    return alive


def _duckduckgo_images(query: str, limit: int = 5) -> List[Dict]:
    """Cari gambar via DuckDuckGo i.js. Return list {url,title,width,height}.
    Kosong bila vqd/i.js diblokir (sering 403 dari IP datacenter)."""
    try:
        r1 = httpx.get(
            "https://duckduckgo.com/",
            params={"q": query, "iax": "images", "ia": "images"},
            headers=_HEADERS, timeout=20, follow_redirects=True,
        )
        m = re.search(r'vqd=([\d-]+)&?', r1.text) or re.search(r'vqd="([^"]+)"', r1.text)
        if not m:
            return []
        r2 = httpx.get(
            "https://duckduckgo.com/i.js",
            params={"l": "id-id", "o": "json", "q": query, "vqd": m.group(1),
                    "f": ",,,", "p": "1"},
            headers={**_HEADERS, "Referer": "https://duckduckgo.com/"},
            timeout=20,
        )
        if r2.status_code != 200:
            return []
        results = json.loads(r2.text).get("results", [])
        return [
            {"url": r.get("image"), "title": r.get("title") or "", "source": "duckduckgo"}
            for r in results[:limit] if r.get("image")
        ]
    except Exception:  # noqa: BLE001 - sumber pertama selalu opsional
        return []


def _wikimedia_images(query: str, limit: int = 6) -> List[Dict]:
    """Cari gambar via API Wikimedia Commons (andal, lisensi bebas).

    Pencarian full-text Commons kaku: query panjang spesifik sering 0 hasil.
    Fallback bertahap - query penuh -> tanpa filter filetype -> 4 kata ->
    3 kata -> 2 kata pertama (ronde 13, terukur: query bahasa Indonesia
    spesifik dari Agent 2 kerap 0 hasil tanpa pelonggaran ini)."""
    terms = query.strip().split()
    attempts = [f"filetype:bitmap {query.strip()}"]
    if len(terms) > 2:
        attempts.append(query.strip())
    for n in (4, 3, 2):
        if len(terms) >= n:
            attempts.append(" ".join(terms[:n]))
    seen_q = set()
    for q in attempts:
        if q in seen_q:
            continue
        seen_q.add(q)
        try:
            r = httpx.get(
                "https://commons.wikimedia.org/w/api.php",
                params={
                    "action": "query", "format": "json", "generator": "search",
                    "gsrsearch": q, "gsrlimit": limit, "gsrnamespace": 6,
                    "prop": "imageinfo", "iiprop": "url|size",
                    "iiurlwidth": _MAX_W_PX,
                },
                headers=_HEADERS, timeout=25,
            )
            pages = (r.json().get("query") or {}).get("pages") or {}
        except Exception:  # noqa: BLE001
            continue
        out = []
        for p in pages.values():
            ii = (p.get("imageinfo") or [{}])[0]
            url = ii.get("thumburl") or ii.get("url")
            if url:
                out.append({"url": url, "title": p.get("title") or "",
                            "source": "wikimedia"})
        if out:
            return out
    return []


def _wiki_get(params: dict, timeout: int = 20):
    """GET API Wikipedia dgn retry singkat utk 429 rate-limit (API Wikimedia
    membatasi request beruntun - terukur ronde 15)."""
    import time

    for attempt in range(2):
        try:
            r = httpx.get("https://en.wikipedia.org/w/api.php", params=params,
                          headers=_HEADERS, timeout=timeout)
            if r.status_code == 429 and attempt == 0:
                time.sleep(5)
                continue
            if r.status_code != 200:
                return None
            return r.json()
        except Exception:  # noqa: BLE001 - sumber opsional
            return None
    return None


def _wikipedia_images(query: str, limit: int = 3) -> List[Dict]:
    """Cari ARTIKEL Wikipedia (bahasa Inggris) lalu ambil gambar utamanya
    (ronde 15). Full-text Commons lemah utk query spesifik, tapi lead image
    artikel topik yang tepat (mis. artikel "Landfill") adalah foto nyata
    yang SANGAT relevan - ini menambal kelemahan terbesar sumber lama."""
    def _titles(q: str) -> List[str]:
        # opensearch json: [query, [judul...], [deskripsi...], [url...]]
        d = _wiki_get({"action": "opensearch", "format": "json", "search": q,
                       "limit": limit, "namespace": 0})
        try:
            return (d or [None, []])[1] or []
        except Exception:  # noqa: BLE001
            return []

    titles = _titles(query.strip())
    if not titles:
        # opensearch AND-match ketat: "landfill weighbridge" = 0 hasil.
        # Longgarkan bertahap sampai ada artikel yang cocok (ronde 15).
        words = query.strip().split()
        for n in (4, 3, 2):
            if len(words) >= n:
                titles = _titles(" ".join(words[:n]))
                if titles:
                    break
    if not titles:
        return []
    d = _wiki_get({"action": "query", "format": "json", "titles": "|".join(titles),
                   "prop": "pageimages", "piprop": "thumbnail",
                   "pithumbsize": _MAX_W_PX})
    out = []
    try:
        pages = (d.get("query") or {}).get("pages") or {}
        for p in pages.values():
            thumb = (p.get("thumbnail") or {}).get("source")
            if thumb:
                out.append({"url": thumb,
                            "title": f"Wikipedia: {p.get('title', '')}",
                            "source": "wikipedia"})
    except Exception:  # noqa: BLE001
        return []
    return out


def search_images(query: str, limit: int = 8) -> List[Dict]:
    """Kandidat gambar utk query: DDG dulu (paling relevan), lalu Wikimedia,
    lalu lead-image artikel Wikipedia (ronde 15)."""
    if not query or not query.strip():
        return []
    return (
        _duckduckgo_images(query, limit=limit)[:limit]
        + _wikimedia_images(query, limit=limit)
        + _wikipedia_images(query, limit=3)
    )


def download_image(cand: Dict, out_path: Path) -> Optional[Dict]:
    """Unduh satu kandidat, validasi PIL, normalisasi ukuran. Return
    {path, page_url, source} atau None bila bukan gambar layak."""
    url = cand.get("url") or ""
    if not url:
        return None
    try:
        r = httpx.get(url, headers=_HEADERS, timeout=30, follow_redirects=True)
        ct = r.headers.get("content-type", "")
        if r.status_code != 200 or "image" not in ct or len(r.content) < 5000:
            return None
        from PIL import Image

        im = Image.open(io.BytesIO(r.content))
        im.load()
        if im.width < _MIN_PX or im.height < _MIN_PX:
            return None
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        # Gambar raksasa diskalakan agar .docx tetap ringan
        if im.width > _MAX_W_PX:
            h = round(im.height * _MAX_W_PX / im.width)
            im = im.resize((_MAX_W_PX, h), Image.LANCZOS)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        im.save(str(out_path), format="PNG")
        return {"path": out_path, "page_url": url, "source": cand.get("source", "")}
    except Exception:  # noqa: BLE001 - kandidat berikutnya
        return None


def fetch_image(query: str, out_path: Path) -> Optional[Dict]:
    """Cari + unduh SATU gambar terbaik utk query. None bila semua gagal
    ATAU tidak ada kandidat yang cukup relevan (ronde 15) - pemanggil
    (word_injector) lalu jatuh ke generate, jadi gambar salah-konteks
    lebih baik TIDAK ADA daripada dipasang.
    Ini fungsi yang dipakai word_injector (cover + gambar per subbab)."""
    if not query or not query.strip():
        return None
    out_path = Path(out_path)
    kws = _keywords(query)
    # Ambang relevansi: >=2 kata kunci cocok dgn judul (atau semua bila
    # kata kuncinya cuma 1). Dulu kandidat pertama langsung dipakai -
    # screenshot Excel/klipart lolos dan dipasang di subbab yang salah.
    # Kandidat Wikipedia cukup 1 kata: artikelnya sendiri sudah dicocokkan
    # mesin pencari, judulnya pendek, dan lead image-nya foto topik yang
    # tepat (jauh lebih baik daripada jatuh generate utk tiap subbab).
    need = min(2, len(kws)) if kws else 0
    for cand in _rank_candidates(search_images(query), kws):
        thr = 1 if cand.get("source") == "wikipedia" else need
        if kws and _relevance(cand, kws) < thr:
            continue
        got = download_image(cand, out_path)
        if got is not None:
            return got
    return None


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    demo = fetch_image(
        "steam power plant condenser",
        Path(__file__).resolve().parent.parent / "runtime" / "test_imgsearch.png",
    )
    print("hasil:", {k: str(v) for k, v in demo.items()} if demo else None)