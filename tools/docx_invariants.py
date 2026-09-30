"""Pemeriksa invarian dokumen .docx hasil produksi (ronde 18).

Menjalankan pemeriksaan yang dipakai untuk membedah `05. Output/01. Komen
untuk Mengoperasikan Unit Pembangkit.docx`, supaya keluhan reviewer tidak
bisa muncul lagi tanpa terdeteksi:

  1. Tidak ada warna biru 00B0F0 di word/document.xml maupun numbering.xml.
     (id=20/27/32 "Knp numbering ny warna biru, fix")
  2. Tidak ada paragraf ber-<w:numPr> yang memuat <w:br/> - tiap baris daftar
     harus paragrafnya sendiri agar Word menomori semuanya.
     (id=22 "Seharusnya ada numbering")
  3. Tidak ada paragraf ber-<w:numPr> yang teksnya berawal ordinal manual.
     (id=44 "Numbering ny kedouble")
  4. Jumlah paragraf body ber-`w:line="240"` = 0 (spacing tidak seragam).
     (id=47 "Spacing ny ga seragam")

Pakai:
    python tools/docx_invariants.py <file.docx> [<file2.docx> ...]
Keluar dengan kode 1 bila ada pelanggaran (bisa dipakai di CI/smoke).
"""

import re
import sys
import zipfile
from pathlib import Path

from docx.oxml import parse_xml
from docx.oxml.ns import qn

_BLUE = "00B0F0"
_ORDINAL_RE = re.compile(r"^\s*\d+[.)]\s+")
_LINE_BREAK_TYPES = (None, "", "textWrapping")


def check(path) -> list:
    """Return daftar pelanggaran (string) untuk satu .docx."""
    path = Path(path)
    violations = []
    with zipfile.ZipFile(path) as z:
        doc_xml = z.read("word/document.xml")
        try:
            num_xml = z.read("word/numbering.xml")
        except KeyError:
            num_xml = b""

    for name, raw in (("document.xml", doc_xml), ("numbering.xml", num_xml)):
        blue = sum(1 for c in parse_xml(raw).findall(".//" + qn("w:color"))
                   if (c.get(qn("w:val")) or "").upper() == _BLUE) if raw else 0
        if blue:
            violations.append(f"{name}: {blue} warna biru {_BLUE} tersisa")

    root = parse_xml(doc_xml)
    for p in root.iter(qn("w:p")):
        pPr = p.find(qn("w:pPr"))
        if pPr is None or pPr.find(qn("w:numPr")) is None:
            continue
        if any((br.get(qn("w:type")) or "").strip() in _LINE_BREAK_TYPES
               for br in p.findall(".//" + qn("w:br"))):
            violations.append("paragraf ber-numPr memuat <w:br/> (numbering "
                              "hanya menomori baris pertama)")
            break
    for p in root.iter(qn("w:p")):
        pPr = p.find(qn("w:pPr"))
        if pPr is None or pPr.find(qn("w:numPr")) is None:
            continue
        text = "".join((t.text or "") for t in p.findall(".//" + qn("w:t")))
        if _ORDINAL_RE.match(text):
            violations.append(f"paragraf ber-numPr berawal ordinal manual: "
                              f"{text.strip()[:50]!r}")
            break

    # Spasi baris 1,0 (line=240) pada paragraf BODY BERISI TEKS -> tidak
    # seragam dengan body lain (line=360). Dua pengecualian yang disengaja:
    #   * paragraf KOSONG - template memakainya sebagai pengatur jarak;
    #   * paragraf ber-indent 0 - blok tanda tangan resmi (Jakarta, <tgl> /
    #     Direktur / NIP) memang rapat dan bukan keluhan reviewer.
    # Yang dicari persis keluhan id=47: paragraf berisi teks BER-indent
    # (dulu `ind left=1134 hanging=425`, `line=240` pada BATASAN VARIABEL).
    line240 = []
    for p in root.iter(qn("w:p")):
        pPr = p.find(qn("w:pPr"))
        sp = pPr.find(qn("w:spacing")) if pPr is not None else None
        if sp is None or (sp.get(qn("w:line")) or "") != "240":
            continue
        ind = pPr.find(qn("w:ind"))
        left = (ind.get(qn("w:left")) or "0") if ind is not None else "0"
        if left in ("0", ""):
            continue
        text = "".join((t.text or "") for t in p.findall(".//" + qn("w:t")))
        if text.strip():
            line240.append(text.strip()[:50])
    if line240:
        violations.append(
            f'{len(line240)} paragraf body berisi teks masih ber-w:line="240" '
            f'(spacing tidak seragam): {line240[:3]}'
        )

    gambar = len(re.findall(r"Gambar \d+\.", " ".join(
        (t.text or "") for t in root.iter(qn("w:t")))))
    return violations, gambar


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        raise SystemExit(2)
    bad = 0
    for a in args:
        v, ngambar = check(a)
        status = "OK" if not v else "PELANGGARAN"
        print(f"[{status}] {Path(a).name} - caption 'Gambar N.'={ngambar}")
        for x in v:
            print(f"    - {x}")
            bad = 1
    raise SystemExit(bad)
