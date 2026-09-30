"""
RAG VERIFY - buktikan index Chroma benar-benar berisi VEKTOR & bisa dicari
=========================================================================
Permintaan user: "make sure it already turns out to vector embedding".

Skrip ini READ-ONLY (tidak menulis/menghapus apa pun) dan keluar dengan
kode 1 bila ada yang tidak sesuai, sehingga bisa dipakai sebagai gerbang
sebelum `docker compose build` (index ikut ter-ship di dalam image).

Yang diperiksa:
1. Identitas embedder aktif + dimensinya.
2. Per collection: jumlah chunk, DIMENSI VEKTOR TERSIMPAN (bukti nyata
   embedding ada - bukan cuma id), skema metadata, rincian per dokumen.
3. Retrievability: query contoh harus mengembalikan sumber yang benar.
4. Bukti end-to-end: rag_retrieve() menggabungkan KETIGA korpus.

Pakai:
    python tools/rag_verify.py
    python tools/rag_verify.py --expect-kb 151
    docker compose exec app python tools/rag_verify.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag import config  # noqa: E402
from tools import vector_store as vs  # noqa: E402

FAILURES: list = []


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"  [FAIL] {msg}")


def _warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def _collections() -> dict:
    """Nama collection -> objek Chroma (hanya yang ADA; read-only)."""
    import chromadb

    client = chromadb.PersistentClient(path=str(vs.CHROMA_DB_DIR))
    wanted = {
        "skkni_kemnaker": vs.COLLECTION_NAME,
        "rag_chat": config.CHAT_COLLECTION,
        "KB": config.KB_COLLECTION,
    }
    out = {}
    for label, name in wanted.items():
        try:
            out[label] = (name, client.get_collection(name))
        except Exception:  # noqa: BLE001 - belum ada = MISSING (bukan error)
            out[label] = (name, None)
    return out


def _stored_dim(col) -> int:
    """Dimensi vektor yang TERSIMPAN di collection (0 bila kosong)."""
    data = col.get(limit=1, include=["embeddings"])
    embs = data.get("embeddings")
    if embs is None or len(embs) == 0:
        return 0
    return len(embs[0])


def main() -> int:
    argv = sys.argv[1:]
    expect_kb = 151
    if "--expect-kb" in argv:
        expect_kb = int(argv[argv.index("--expect-kb") + 1])

    print("=" * 68)
    print("RAG VERIFY - Chroma embeddings (read-only)")
    print("=" * 68)
    print(f"DB          : {vs.CHROMA_DB_DIR}")
    print(f"KB collection: {config.KB_COLLECTION} | top_k KB: {config.RAG_TOP_K_KB}")

    # --- 1) Embedder ---------------------------------------------------
    print("\n[1] Embedder aktif")
    embedder = vs.get_embedder()
    probe = embedder.embed_query("probe dimensi embedding")
    model_name = getattr(embedder, "model_name", "?")
    print(f"  model : {model_name}")
    print(f"  dim   : {len(probe)}")
    if len(probe) != 384:
        _fail(f"dimensi embedder aktif {len(probe)} != 384. Index chroma_db di-ship "
              "dengan all-MiniLM 384-d - kosongkan EMBEDDING_MODEL/EMBEDDING_BASE_URL "
              "lalu rebuild, atau RAG akan rusak senyap.")
    else:
        _ok("embedder lokal 384-d (cocok dengan index yang di-ship)")

    # --- 2) Census -----------------------------------------------------
    print("\n[2] Census collection (jumlah chunk + dimensi vektor tersimpan)")
    cols = _collections()
    for label, (name, col) in cols.items():
        if col is None:
            if label == "KB":
                _fail(f"collection '{name}' BELUM ADA - jalankan: python -m rag.kb")
            else:
                _fail(f"collection '{name}' tidak ditemukan di {vs.CHROMA_DB_DIR}")
            continue
        count = col.count()
        dim = _stored_dim(col)
        print(f"\n  {label}: '{name}'")
        print(f"    chunk          : {count}")
        print(f"    dimensi vektor : {dim}")
        if count == 0:
            _fail(f"{name}: kosong (tidak ada vektor)")
            continue
        if dim == 0:
            _fail(f"{name}: tidak ada embedding tersimpan")
        elif dim != len(probe):
            _fail(f"{name}: dimensi tersimpan {dim} != embedder aktif {len(probe)}")
        else:
            _ok(f"{name}: {count} chunk, vektor {dim}-d")

        metas = [m or {} for m in (col.get(limit=3, include=["metadatas"]).get("metadatas") or [])]
        keys = sorted({k for m in metas for k in m})
        print(f"    metadata keys  : {keys}")

        if label == "KB":
            grouped = {}
            for m in (col.get(include=["metadatas"]).get("metadatas") or []):
                m = m or {}
                g = grouped.setdefault(m.get("doc_id", "?"), {
                    "source_type": m.get("source_type", "?"), "n": 0,
                    "embedder": m.get("embedder", "?"), "dim": m.get("dim", "?"),
                })
                g["n"] += 1
            for doc_id, g in sorted(grouped.items()):
                print(f"    {doc_id:24s} {g['n']:>4} chunk  ({g['source_type']}, "
                      f"embedder={g['embedder']}, dim={g['dim']})")
            if count != expect_kb:
                _warn(f"{name}: {count} chunk != perkiraan {expect_kb} "
                      "(boleh beda bila isi contoh modul berubah)")
            st = {g["source_type"] for g in grouped.values()}
            for want in ("template", "contoh_modul"):
                if want in st:
                    _ok(f"KB memuat source_type '{want}'")
                else:
                    _fail(f"KB tidak memuat source_type '{want}'")

    # --- 3) Retrievability --------------------------------------------
    print("\n[3] Retrievability - query contoh harus mengembalikan sumber yang benar")
    # KB diuji lewat jalur NYATA (rag.kb.kb_retrieve) supaya aturan jaminan
    # slot template ikut terverifikasi, bukan hanya kueri mentah Chroma.
    from rag.kb import kb_retrieve

    kb_hits = kb_retrieve("bagian apa saja yang wajib ada dalam modul pelatihan kemnaker",
                          k=config.RAG_TOP_K_KB)
    kb_types = [h.get("source_type") for h in kb_hits]
    if "template" in kb_types:
        _ok(f"KB: template tersedia di top-{config.RAG_TOP_K_KB} -> {kb_types}")
    else:
        _fail(f"KB: template TIDAK terambil (dapat {kb_types}) - "
              "cek ENSURE_SOURCE_TYPE / RAG_TOP_K_KB")

    kb_contoh = kb_retrieve("contoh gaya penulisan modul pelatihan", k=config.RAG_TOP_K_KB)
    if "contoh_modul" in [h.get("source_type") for h in kb_contoh]:
        _ok("KB: contoh_modul terambil")
    else:
        _fail("KB: contoh_modul tidak terambil")

    for label, query, want in [
        ("skkni_kemnaker", "elemen kompetensi unit skkni", "skkni"),
        ("rag_chat", "program pelatihan PLTSa", "program"),
    ]:
        name, col = cols.get(label, (None, None))
        if col is None or col.count() == 0:
            _fail(f"{label}: tidak bisa diuji (collection kosong/hilang)")
            continue
        emb = embedder.embed_query(query)
        res = col.query(query_embeddings=[emb], n_results=3,
                        include=["documents", "metadatas", "distances"])
        metas = (res.get("metadatas") or [[]])[0] or []
        dists = (res.get("distances") or [[]])[0] or []
        # skkni_kemnaker tidak menyimpan source_type di metadata -> label dari nama
        got = [(m or {}).get("source_type") or ("skkni" if label == "skkni_kemnaker" else "?")
               for m in metas]
        top = f"{got[0]} ({1.0 - float(dists[0]):.3f})" if got and dists else "-"
        if want in got:
            _ok(f"{label}: '{query[:44]}' -> {top}")
        else:
            _fail(f"{label}: '{query[:44]}' -> {got} (tidak memuat '{want}')")

    # --- 4) Bukti end-to-end ------------------------------------------
    print("\n[4] End-to-end rag_retrieve() - ketiga korpus harus tergabung")
    from rag.retriever import rag_retrieve

    hits = rag_retrieve("struktur dan gaya penulisan modul pelatihan kemnaker", k=8)
    hist: dict = {}
    for h in hits:
        hist[h.get("source_type", "?")] = hist.get(h.get("source_type", "?"), 0) + 1
    print(f"  {len(hits)} hit, histogram source_type: {hist}")
    for h in hits[:5]:
        print(f"    [{h.get('score', 0):.3f}] {h.get('source_type')}: "
              f"{h.get('source')} - {h.get('content', '')[:70]}...")
    for want in ("skkni", "program"):
        if want in hist:
            _ok(f"korpus '{want}' ikut terambil")
        else:
            _fail(f"korpus '{want}' tidak muncul di hasil gabungan")
    if any(k in hist for k in ("template", "contoh_modul")):
        _ok("korpus KB (template/contoh_modul) ikut terambil")
    else:
        _warn("KB tidak muncul di top-8 query ini (bisa wajar bila SKKNI/program "
              "lebih relevan) - cek /api/rag/chat/stream dgn pertanyaan soal template")

    print("\n" + "=" * 68)
    if FAILURES:
        print(f"GAGAL - {len(FAILURES)} masalah:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("SEMUA PEMERIKSAAN LULUS - embedding ada, tersimpan, dan bisa dicari.")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
