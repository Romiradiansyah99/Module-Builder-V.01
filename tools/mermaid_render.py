"""
MERMAID RENDERER - Ubah skrip Mermaid sederhana menjadi gambar PNG
=================================================================
Ronde 11: user menuntut gambar/flowchart TETAP tersedia di .docx final
("gambar, flowchart diagram, dll. nya ini harus juga tersedia"). VPS tidak
bisa pasang Node.js/mmdc (DNS outbound terblokir, disk sempit), jadi
renderer ini pure-Python (Pillow) untuk subset Mermaid yang dipakai
Agent 2: `flowchart TD|TB|LR` / `graph TD|TB|LR` dengan edge `-->` dan
label kotak `A[Teks]`, `A(Label)`, `A{Label}`.

Font memakai ImageFont.load_default(size=) (font Aileron tersemat Pillow
>=10.1) sehingga TIDAK butuh file font di server.

Jalankan demo:
    python tools/mermaid_render.py
"""

import math
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Palet warna ala Mermaid default
_FILL = (236, 236, 255)       # kotak: #ECECFF
_BORDER = (147, 112, 219)     # kotak: #9370DB
_EDGE = (51, 51, 51)
_TEXT = (33, 33, 33)
_BG = (255, 255, 255)

_BOX_MIN_W = 150
_BOX_PAD_X = 26
_BOX_PAD_Y = 16
_LAYER_GAP_X = 90
_LAYER_GAP_Y = 90
_MAX_TEXT_W = 300


def _load_font(size: int):
    # Pillow >= 10.1: load_default(size=) memuat font Aileron bawaan,
    # tanpa file .ttf di disk (server VPS tidak punya font ekstra).
    return ImageFont.load_default(size=size)


def _wrap_text(text: str, font, max_w: int) -> list:
    """Pecah teks jadi beberapa baris agar muat di dalam kotak."""
    words = text.split()
    if not words:
        return [""]
    lines, cur = [], words[0]
    for w in words[1:]:
        trial = cur + " " + w
        if font.getlength(trial) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


# --- Parsing ---------------------------------------------------------------

_EDGE_RE = re.compile(
    r"""^\s*(?P<src>[A-Za-z0-9_]+)(?:\[[^\]]*\]|\([^)]*\)|\{[^}]*\})?
        \s*(?:-->|-?->|-\.->|==>)
        (?:\|(?P<label>[^|]*)\|\s*)?
        (?P<dst>[A-Za-z0-9_]+)(?:\[[^\]]*\]|\([^)]*\)|\{[^}]*\})?\s*$""",
    re.VERBOSE,
)

_NODE_RE = re.compile(r"^\s*(?P<id>[A-Za-z0-9_]+)(?P<shape>[\[({])(?P<label>[^\][(){}]*)[\])}]\s*$")


def _parse_mermaid(script: str):
    """Skrip mermaid -> (direction, nodes{id: label}, edges[(src, dst, label)]).
    Baris tak-dikenal diabaikan (renderer bersifat best-effort)."""
    direction = "TD"
    nodes: dict = {}
    edges: list = []
    for raw in script.splitlines():
        line = raw.strip()
        if not line or line.startswith("%%"):
            continue
        m = re.match(r"^(?:flowchart|graph)\s+(TD|TB|LR|RL|BT)\b", line, re.IGNORECASE)
        if m:
            d = m.group(1).upper()
            direction = "LR" if d in ("LR", "RL") else "TD"
            continue
        em = _EDGE_RE.match(line)
        if em:
            src, dst, label = em.group("src"), em.group("dst"), em.group("label") or ""
            edges.append((src, dst, label.strip()))
            nodes.setdefault(src, src)
            nodes.setdefault(dst, dst)
            continue
        nm = _NODE_RE.match(line)
        if nm:
            nodes[nm.group("id")] = nm.group("label").strip() or nm.group("id")
            continue
        # pola berantai "A --> B[Label] --> C": pecah per panah
        if "-->" in line:
            parts = [p.strip() for p in line.split("-->") if p.strip()]
            ids = []
            ok = True
            for part in parts:
                part = re.sub(r"^\|[^|]*\|", "", part).strip()
                m2 = re.match(r"^([A-Za-z0-9_]+)", part)
                if not m2:
                    ok = False
                    break
                ids.append(m2.group(1))
                nm2 = _NODE_RE.match(part)
                if nm2:
                    nodes[nm2.group("id")] = nm2.group("label").strip() or nm2.group("id")
                else:
                    nodes.setdefault(ids[-1], ids[-1])
            if ok and len(ids) >= 2:
                for a, b in zip(ids, ids[1:]):
                    edges.append((a, b, ""))
                    nodes.setdefault(a, a)
                    nodes.setdefault(b, b)
    return direction, nodes, edges


def _layerize(nodes: dict, edges: list) -> list:
    """Bagi node ke lapisan topologis (kedalaman jalur terpanjang)."""
    depth = {nid: 0 for nid in nodes}
    changed = True
    guard = 0
    while changed and guard < len(nodes) + 2:
        changed = False
        guard += 1
        for src, dst, _ in edges:
            if dst in depth and src in depth and depth[src] + 1 > depth[dst]:
                depth[dst] = depth[src] + 1
                changed = True
    layers: dict = {}
    for nid, d in depth.items():
        layers.setdefault(d, []).append(nid)
    return [layers[k] for k in sorted(layers)]


def _node_metrics(node_ids: list, labels: dict, font) -> dict:
    """Ukur semua kotak: {id: (w, h, lines)}."""
    out = {}
    for nid in node_ids:
        label = labels.get(nid, nid)
        lines = _wrap_text(label, font, _MAX_TEXT_W)
        tw = max(font.getlength(ln) for ln in lines) if lines else 0
        w = max(_BOX_MIN_W, int(tw) + 2 * _BOX_PAD_X)
        h = _BOX_PAD_Y * 2 + len(lines) * int(font.size * 1.35)
        out[nid] = (w, h, lines)
    return out


def _render_mermaid_ink(code: str, out_path: Path) -> Path | None:
    """Render via mermaid.ink (renderer Mermaid ASLI di internet, ronde 13).

    Kroki.io tak terjangkau dari VPS (000), jadi layanan ini dipakai:
    GET https://mermaid.ink/img/<base64url>?type=png -> PNG resmi mermaid
    (layout, diamond keputusan, arrowhead rapi - kualitas jauh di atas
    renderer Pillow lokal). Return None bila jaringan/render gagal, agar
    pemanggil jatuh ke renderer lokal - kegagalan jaringan TIDAK boleh
    menghilangkan gambar dari dokumen.
    """
    import base64

    import httpx

    try:
        b64 = base64.urlsafe_b64encode(code.strip().encode("utf-8")).decode("ascii")
        resp = httpx.get(
            f"https://mermaid.ink/img/{b64}?type=png", timeout=45,
            follow_redirects=True,
        )
        if resp.status_code != 200 or len(resp.content) < 1000:
            return None
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(resp.content)
        # Validasi PNG benar-benar bisa dibuka (jangan sampai menyimpan
        # halaman error HTML / gambar 1x1 seperti kasus image10.png 70 byte).
        from PIL import Image

        with Image.open(str(out_path)) as im:
            im.load()
            if im.width < 100 or im.height < 100:
                return None
        return out_path
    except Exception:  # noqa: BLE001 - fallback renderer lokal
        return None


def render_mermaid(code: str, out_path: Path = None) -> Path:
    """Render blok skrip Mermaid (boleh masih dibungkus ```mermaid) -> PNG.
    Ronde 13: PRIORITAS mermaid.ink (render Mermaid asli); renderer Pillow
    lokal hanya fallback saat layanan tak terjangkau. Return path PNG;
    raise ValueError kalau tidak ada node/edge yang bisa diparse."""
    if "```" in code:
        m = re.search(r"```(?:mermaid)?\s*(.*?)```", code, re.DOTALL)
        if m:
            code = m.group(1)
    if out_path is None:
        out_path = Path(__file__).resolve().parent.parent / "runtime" / "mermaid_tmp.png"
    out_path = Path(out_path)
    # Ronde 13: layanan mermaid.ink dulu - render Mermaid ASLI (diamond,
    # arrowhead, layout graphviz-quality). Gagal jaringan -> renderer lokal.
    ink = _render_mermaid_ink(code, out_path)
    if ink is not None:
        return ink
    direction, nodes, edges = _parse_mermaid(code)
    if not nodes:
        raise ValueError("Skrip Mermaid tidak berisi node yang dikenali.")
    if not edges:
        edges = []

    font = _load_font(26)
    efont = _load_font(22)
    layers = _layerize(nodes, edges)
    metrics = _node_metrics(list(nodes), nodes, font)

    # Layout generik: lapisan tersusun sepanjang sumbu PRIMER; node di dalam
    # satu lapisan tersusun sepanjang sumbu SEKUNDER.
    #   TD : primer = Y (baris), sekunder = X (kolom)
    #   LR : primer = X (kolom), sekunder = Y (baris)
    horiz = direction == "LR"
    prim = [max(metrics[n][0 if horiz else 1] for n in layer) for layer in layers]
    sec = []
    for layer in layers:
        for i, nid in enumerate(layer):
            while len(sec) <= i:
                sec.append(0)
            sec[i] = max(sec[i], metrics[nid][1 if horiz else 0])
    n_sec = max(len(l) for l in layers)
    gap_p = _LAYER_GAP_X if horiz else _LAYER_GAP_Y
    gap_s = _LAYER_GAP_Y if horiz else _LAYER_GAP_X
    total_p = sum(prim) + gap_p * (len(prim) - 1) + 80
    total_s = sum(sec) + gap_s * (n_sec - 1) + 80
    w = int(max(total_p, 500)) if horiz else int(max(total_s, 500))
    h = int(max(total_s, 300)) if horiz else int(max(total_p, 300))

    img = Image.new("RGB", (w, h), _BG)
    drw = ImageDraw.Draw(img)
    pos = {}
    p_cursor = 40
    for li, layer in enumerate(layers):
        s_cursor = 40
        for i, nid in enumerate(layer):
            if horiz:
                cx, cy = p_cursor + prim[li] // 2, s_cursor + sec[i] // 2
            else:
                cx, cy = s_cursor + sec[i] // 2, p_cursor + prim[li] // 2
            pos[nid] = (cx, cy)
            _draw_node(drw, nid, (cx, cy), metrics[nid], font)
            s_cursor += sec[i] + gap_s
        p_cursor += prim[li] + gap_p
    _draw_edges(drw, edges, pos, metrics, efont, horiz)

    if out_path is None:
        out_path = Path(__file__).resolve().parent.parent / "runtime" / "mermaid_tmp.png"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG")
    return out_path


def _draw_node(drw, nid, center, metrics, font) -> None:
    w, h, lines = metrics
    x0 = center[0] - w // 2
    y0 = center[1] - h // 2
    drw.rounded_rectangle([x0, y0, x0 + w, y0 + h], radius=10, fill=_FILL, outline=_BORDER, width=3)
    ty = y0 + _BOX_PAD_Y
    for ln in lines:
        tw = font.getlength(ln)
        drw.text((center[0] - tw / 2, ty), ln, font=font, fill=_TEXT)
        ty += int(font.size * 1.35)


def _draw_edges(drw, edges, pos, metrics, efont, horiz: bool) -> None:
    for src, dst, label in edges:
        if src not in pos or dst not in pos:
            continue
        if horiz:
            start = (pos[src][0] + metrics[src][0] // 2, pos[src][1])
            end = (pos[dst][0] - metrics[dst][0] // 2, pos[dst][1])
        else:
            start = (pos[src][0], pos[src][1] + metrics[src][1] // 2)
            end = (pos[dst][0], pos[dst][1] - metrics[dst][1] // 2)
        if src == dst:  # loop ke diri sendiri -> lingkaran kecil
            r = 24
            bx, by = start
            drw.arc([bx - r, by - 2 * r, bx + r, by], 200, 80, fill=_EDGE, width=3)
            continue
        drw.line([start, end], fill=_EDGE, width=3)
        _draw_arrowhead(drw, start, end)
        if label:
            mx, my = (start[0] + end[0]) // 2, (start[1] + end[1]) // 2
            tw = efont.getlength(label)
            pad = 6
            drw.rectangle([mx - tw / 2 - pad, my - efont.size, mx + tw / 2 + pad, my + 4], fill=_BG)
            drw.text((mx - tw / 2, my - efont.size), label, font=efont, fill=_TEXT)


def _draw_arrowhead(drw, start, end, size: int = 12) -> None:
    ax, ay = start
    bx, by = end
    ang = math.atan2(by - ay, bx - ax)
    for off in (math.radians(25), -math.radians(25)):
        drw.line(
            [end, (bx - size * math.cos(ang + off), by - size * math.sin(ang + off))],
            fill=_EDGE,
            width=3,
        )


if __name__ == "__main__":
    demo = """
flowchart TD
A[Mulai] --> B[Merencanakan pekerjaan]
B --> C[Menyiapkan sarana dan prasarana]
C --> D[Melaksanakan pekerjaan sesuai SOP]
D --> E[Membuat laporan hasil pekerjaan]
E --> F[Selesai]
"""
    p = render_mermaid(demo)
    print(f"[mermaid_render] demo TD -> {p}")
    demo2 = """
flowchart LR
A[Mulai] --> B[Cek alat]
B --> C[Kerjakan]
"""
    p2 = render_mermaid(demo2, out_path=Path(__file__).resolve().parent.parent / "runtime" / "mermaid_lr.png")
    print(f"[mermaid_render] demo LR -> {p2}")