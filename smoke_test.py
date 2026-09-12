"""
SMOKE TEST - Validasi semua komponen sistem secara berurutan
============================================================
Jalankan:  python smoke_test.py            (tanpa LLM: infrastruktur saja)
           python smoke_test.py --full    (termasuk 1 run LLM end-to-end kecil)

Langkah yang dites:
 1. Config .env termuat benar
 2. Koneksi LLM (ChatOllama) - chat ping
 3. Koneksi embedding cloud (kandidat model otomatis)
 4. Ingest RAG SKKNI (skip jika sudah ada index)
 5. Retrieve SKKNI bekerja
 6. Penemuan tag template docx
 7. Ekstraksi teks program pelatihan & contoh modul
 8. Topologi graph LangGraph terbangun + interrupt HITL terdaftar
 8b. Unit: parser, reducer, routing revisi
 8c. (--full) Auto-effort + dig percakapan LLM (ronde 3)
 9. Demo inject docxtpl (tanpa LLM)
10. (--full) Agent 3 structured output + mini pipeline 1 modul
"""

import sys
import traceback

# Konsol Windows default cp1252: emoji dari balasan LLM (😊 dst.) bikin
# UnicodeEncodeError. Paksa UTF-8 + replace agar print tidak pernah meledak.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - stream non-reconfigurable diabaikan
        pass

PASS, FAIL = "  [OK]", "  [GAGAL]"
failures = 0


def step(name, func):
    global failures
    print(f"\n=== {name} ===")
    try:
        func()
        print(PASS, name)
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(FAIL, name)
        traceback.print_exc()
        print(f"      -> {exc}")


print("=" * 60)
print("SMOKE TEST - Kemnaker AI Module Builder")
print("=" * 60)

# --- 1. Config ---------------------------------------------------------
def t_config():
    import os

    from dotenv import load_dotenv

    load_dotenv()
    assert os.getenv("LLM_MODEL"), "LLM_MODEL kosong"
    assert os.getenv("LLM_BASE_URL"), "LLM_BASE_URL kosong"
    print(f"      LLM: {os.getenv('LLM_MODEL')} @ {os.getenv('LLM_BASE_URL')}")
    emb = os.getenv("EMBEDDING_MODEL", "")
    print(f"      Kandidat embedding: {emb}")


step("1. Konfigurasi .env", t_config)


# --- 2. LLM chat ping --------------------------------------------------
def t_llm():
    from tools.llm_config import get_llm

    # num_predict >= 800: glm reasoning model - fase "thinking" memakan budget
    # token secara diam; budget kecil (50) menghasilkan konten kosong.
    llm = get_llm(temperature=0.0, num_predict=800)
    r = llm.invoke("Balas dengan satu kata saja: PONG")
    assert r.content, "LLM merespons kosong (budget num_predict terlalu kecil utk thinking?)"
    # _strip_think() wajib membuang blok thinking yang bocor ke content.
    assert "think" not in r.content.lower(), f"blok thinking bocor ke content: {r.content[:120]!r}"
    print(f"      Respons: {r.content[:100]}")


step("2. Koneksi LLM (ChatOllama)", t_llm)


# --- 3. Embedding cloud ------------------------------------------------
def t_embed():
    from tools.vector_store import get_embedder

    e = get_embedder()
    v = e.embed_query("instalasi pembangkit listrik tenaga surya")
    assert len(v) > 0, "vektor embedding kosong"
    print(f"      Model aktif: {e.model_name} | dimensi: {len(v)}")


step("3. Koneksi embedding cloud", t_embed)


# --- 4. Ingest RAG ------------------------------------------------------
def t_ingest():
    from tools.vector_store import get_collection, ingest_skkni_docs

    ingest_skkni_docs()
    n = get_collection().count()
    assert n > 0, "collection ChromaDB kosong"
    print(f"      Chunk terindeks: {n}")


step("4. Ingest RAG SKKNI", t_ingest)


# --- 5. Retrieve ---------------------------------------------------------
def t_retrieve():
    from tools.vector_store import get_retriever

    docs = get_retriever(top_k=3)("unit kompetensi pembangkit listrik")
    assert docs, "retriever tidak mengembalikan apa pun"
    print(f"      {len(docs)} dokumen | top score: {docs[0]['score']:.3f}")
    print(f"      Contoh: [{docs[0]['source']}, hal. {docs[0]['page']}] {docs[0]['content'][:120]}...")


step("5. Retrieve SKKNI", t_retrieve)


# --- 6. Template tags ----------------------------------------------------
def t_template():
    from tools.doc_utils import get_template_tags, template_info

    tags = get_template_tags()
    info = template_info()
    assert info["exists"], f"template tidak ada: {info['path']}"
    # BLIND SPOT lama: langkah ini hanya mencetak jumlah tag - template contoh
    # tanpa satu pun {{ tag }} lolos padahal Agent 2 pasti crash. Sekarang wajib.
    assert len(tags) > 0, f"template TIDAK PUNYA tag {{ }} - jalankan: python tools/template_builder.py"
    assert "judul_modul" in tags, "tag utama 'judul_modul' hilang dari template"
    print(f"      Template: {info['path']}")
    print(f"      {len(tags)} tag: {', '.join(tags[:15])}{' ...' if len(tags) > 15 else ''}")


step("6. Tag template docx", t_template)


# --- 7. Konteks program & contoh modul -----------------------------------
def t_context():
    from tools.doc_utils import get_example_module_text, get_program_text

    prog = get_program_text(max_chars=3000)
    ex = get_example_module_text()
    assert len(prog) > 50, "teks program pelatihan kosong - periksa PROGRAM_DOC_PATH di .env"
    print(f"      Program: {len(get_program_text())} karakter")
    print(f"      Contoh modul: {len(ex)} karakter")
    print(f"      Cuplikan program: {prog[:150]}...")


step("7. Teks program & contoh modul", t_context)


# --- 7b. Ekstraksi unit kompetensi (deterministik, tanpa LLM) ---------------
def t_units():
    from tools.unit_extractor import (
        extract_program_meta,
        extract_program_units,
        extract_unit_syllabus_rows,
        match_unit,
        rows_status,
    )

    units = list(extract_program_units())
    assert len(units) >= 18, f"unit kurang: {len(units)}"
    u = next(x for x in units if x["no"] == "1.1")
    assert u["judul"] == "Memproses Penerimaan Sampah", f"judul: {u['judul']!r}"
    assert u["kode"] == "E.38SPH02.014.01", f"kode: {u['kode']!r}"
    assert "JP" in u["alokasi"], f"alokasi: {u['alokasi']!r}"
    assert extract_program_meta().get("judul"), "judul program kosong"

    rows = extract_unit_syllabus_rows().get("1.1", ())
    assert len(rows) >= 3, f"rows 1.1 kurang: {len(rows)}"
    assert all(r["kuk"] and r["durasi"] for r in rows), "ada baris kuk/durasi kosong"

    assert rows_status("2.3") == "none", "unit 2.3 harusnya tanpa tabel acuan"
    snapped = match_unit(units, "memproses penerimaan sampah ")
    assert snapped and snapped["no"] == "1.1", "match_unit gagal snap judul"
    print(f"      {len(units)} unit | 1.1={u['judul']!r} | rows 1.1={len(rows)} | status 2.3={rows_status('2.3')}")


step("7b. Ekstraksi unit kompetensi", t_units)


# --- 8. Graph topology ----------------------------------------------------
def t_graph():
    from main_graph import build_graph

    graph = build_graph()
    print(graph.get_graph().draw_ascii())
    snap = graph  # compile sukses = topologi valid
    assert snap is not None


step("8. Topologi LangGraph", t_graph)


# --- 8b. Unit: parser JSON, reducer, routing revisi -------------------------
def t_unit():
    # a) parse_modules_json v2: tahan fence; judul di-snap ke daftar unit dan
    #    rows TABEL ACUAN docx menang atas rows LLM (unit 1.1 docx-backed).
    from agents.agent1_syllabus import parse_modules_json, rows_to_markdown

    raw = ('```json\n{"mode": "build", "program_name": "PLTSa", "reply": "ok", "modules": ['
           '{"module_title": "memproses penerimaan sampah", "kode_unit": "", "alokasi_waktu": "",'
           ' "syllabus_rows": [{"elemen_no": "9.", "elemen": "E1", "kuk_no": "9.1", "kuk": "K1",'
           ' "indikator": "I1", "pengetahuan": "P1", "keterampilan": "S1", "durasi": "3 JP"}]}'
           ']}\n```')
    name, mods = parse_modules_json(raw)
    assert name == "PLTSa" and len(mods) == 1
    m0 = mods[0]
    assert m0["module_id"] == "M01"
    assert m0["module_title"] == "Memproses Penerimaan Sampah", f"snap gagal: {m0['module_title']!r}"
    assert m0["kode_unit"] == "E.38SPH02.014.01"
    assert m0["syllabus_rows"], "rows docx hilang"
    assert m0["syllabus_rows"][0]["kuk"] != "K1", "rows docx harusnya menang atas rows LLM"
    assert "| 1.1" in m0["syllabus_content"], "syllabus_content tidak diturunkan dari rows"

    # a2) judul tak dikenal -> rows LLM dipakai apa adanya (path unit gap)
    raw2 = ('```json\n{"mode": "build", "program_name": "X", "reply": "", "modules": ['
            '{"module_title": "Uji Unit Fiktif", "kode_unit": "K.X", "alokasi_waktu": "8 JP",'
            ' "syllabus_rows": [{"elemen_no": "1.", "elemen": "E1", "kuk_no": "1.1", "kuk": "K1",'
            ' "indikator": "I1", "pengetahuan": "P1", "keterampilan": "S1", "durasi": "3 JP"}]}'
            ']}\n```')
    _, mods2 = parse_modules_json(raw2)
    assert mods2[0]["module_title"] == "Uji Unit Fiktif"
    assert mods2[0]["syllabus_rows"][0]["kuk"] == "K1", "rows LLM harus dipakai utk unit tanpa acuan"
    assert "| 1.1 K1" in mods2[0]["syllabus_content"]

    # b) rows_to_markdown round-trip & mode "ask" (modul kosong) tak meledak
    md = rows_to_markdown([{"elemen_no": "1.", "elemen": "E", "kuk_no": "1.1", "kuk": "K",
                            "indikator": "I", "pengetahuan": "P", "keterampilan": "S", "durasi": "3 JP"}])
    assert "| 1. E | 1.1 K |" in md
    from agents.agent1_syllabus import ASK_INTENT_RE, _scope_answered, SCOPE_MARKER

    assert _scope_answered([{"role": "assistant", "content": f"x {SCOPE_MARKER}"},
                            {"role": "user", "content": "buat semua unit"}])
    assert not _scope_answered([{"role": "assistant", "content": f"x {SCOPE_MARKER}"},
                                {"role": "user", "content": "halo"}])
    assert not _scope_answered([{"role": "user", "content": "buat semua unit"}])
    # ronde 3: "lanjut" = perintah membangun; pembuka umum bukan intent
    assert ASK_INTENT_RE.search("lanjut"), '"lanjut" harus jadi build intent'
    assert ASK_INTENT_RE.search("lanjutkan saja ya")
    assert not ASK_INTENT_RE.search("cara buat kompos yang baik?"), 'pertanyaan umum jangan trigger build'
    assert not ASK_INTENT_RE.search("mau tanya dulu")
    assert not ASK_INTENT_RE.search("aku mau bikin modul pelatihan nih"), 'pembuka "bikin" harus tetap masuk dig'

    # b2) pengetahuan bernomor urut "1. " (feedback ronde 2 item 2)
    from tools.doc_utils import number_pengetahuan

    assert number_pengetahuan("Penjelasan tentang:\n1. A\n3. B") == "Penjelasan tentang:\n1. A\n2. B"
    assert number_pengetahuan("X\nY") == "1. X\n2. Y"
    assert number_pengetahuan("") == ""
    # rows hasil _normalize_rows otomatis bernomor
    from agents.agent1_syllabus import _normalize_rows

    nr = _normalize_rows([{"elemen_no": "1.", "elemen": "E", "kuk_no": "1.1", "kuk": "K",
                           "indikator": "I", "pengetahuan": "A\nB", "keterampilan": "S", "durasi": "3 JP"}])
    assert nr[0]["pengetahuan"] == "1. A\n2. B", nr[0]["pengetahuan"]

    # b3) ahli JP: total durasi baris = alokasi unit (feedback ronde 2 item 5)
    from agents.agent1_syllabus import _apply_jp_rules

    rows_jp = [{"durasi": "5 JP"}, {"durasi": "5 JP"}, {"durasi": "5 JP"}, {"durasi": "5 JP"}]
    out, alo = _apply_jp_rules([dict(r) for r in rows_jp], "16 JP")
    assert sum(int(r["durasi"].split()[0]) for r in out) == 16, out
    assert alo == "16 JP @ 45 menit", alo
    out2, alo2 = _apply_jp_rules([{"durasi": ""}, {"durasi": "2 JP"}], "")
    assert alo2 == "5 JP @ 45 menit", alo2
    assert all(int(r["durasi"].split()[0]) >= 1 for r in out2)

    # c) merge_modules: upsert by module_id tanpa duplikasi
    from agents.state import ModuleState, merge_modules, make_module

    a = make_module("M01", "A", "syl-a")
    b = make_module("M02", "B", "syl-b")
    a2 = make_module("M01", "A-revisi", "syl-a2")
    merged = merge_modules([a, b], [a2])
    assert len(merged) == 2, f"duplikasi! {len(merged)}"
    assert next(m for m in merged if m["module_id"] == "M01")["module_title"] == "A-revisi"

    # d) route_after_agent3: FAIL -> Send ke Agent2; PASS & forced-PASS -> fan-in.
    #    (Forced-PASS di iterasi ke-3 dihasilkan agent3_node sbg "PASS (Need Human Review)")
    from main_graph import route_after_agent3
    from agents.state import STATUS_NEED_HUMAN_REVIEW
    from langgraph.types import Send

    m1 = make_module("M01", "A", "s"); m1["status_evaluasi"] = "FAIL"; m1["iteration_count"] = 1
    mp = make_module("M01", "A", "s"); mp["status_evaluasi"] = "PASS"; mp["iteration_count"] = 1
    mr = make_module("M01", "A", "s"); mr["status_evaluasi"] = STATUS_NEED_HUMAN_REVIEW; mr["iteration_count"] = 3
    assert isinstance(route_after_agent3({"modules": [m1]}), Send), "FAIL harus loop ke Agent2"
    assert route_after_agent3({"modules": [mp]}) == "Inject_Word", "PASS harus fan-in"
    assert route_after_agent3({"modules": [mr]}) == "Inject_Word", "forced-PASS harus fan-in"

    # e) agent2._extract_json_object: tahan fence
    from agents.agent2_content import _extract_json_object

    obj = _extract_json_object('```json\n{"judul_modul": "X", "elemen_rows": [{"elemen": "E"}]}\n```')
    assert obj["judul_modul"] == "X" and obj["elemen_rows"][0]["elemen"] == "E"

    print("      parse v2 (snap judul + derive markdown) / merge / routing / extract_json: OK")


step("8b. Unit: parser, reducer, routing revisi", t_unit)


# --- 8c. Auto-effort + dig percakapan LLM (ronde 3) --------------------------
def t_dig():
    # a) Preset effort & policy AUTO
    from tools.llm_config import auto_effort, get_llm_for_effort

    assert get_llm_for_effort("low").num_predict == 4000
    assert get_llm_for_effort("medium").num_predict == 8000
    assert get_llm_for_effort("high").num_predict == 16000
    # ronde 11b: extra naik 24000 -> 48000 (draf produksi nyata >60k char,
    # 24000 token menembus budget dan memotong JSON di tengah)
    assert get_llm_for_effort("extra").num_predict == 48000
    assert auto_effort("agent1.dig") == "low"
    assert auto_effort("agent1.build") == "high"
    assert auto_effort("agent1.build", draft=True) == "extra"
    assert auto_effort("agent2") == "extra"
    assert auto_effort("agent3") == "medium"

    # b) Agent 1 node dig LLM MURNI: sapaan umum -> balasan konsultan yang
    #    menanyakan kebutuhan (bukan teks canned _ask_reply).
    from agents.agent1_syllabus import SCOPE_MARKER, _ask_reply, agent1_node
    from tools.unit_extractor import extract_program_meta, extract_program_units

    units = list(extract_program_units())
    history = [{"role": "user", "content": "Halo, aku mau bikin modul pelatihan nih"}]
    upd = agent1_node({"chat_history": list(history), "modules": []})
    mods = upd["modules"]
    assert not mods, f"mode dig tak boleh menghasilkan modul: {len(mods)}"
    msgs = upd["chat_history"]
    assert msgs and msgs[-1]["role"] == "assistant"
    reply = msgs[-1]["content"]
    assert reply.strip(), "reply dig kosong"
    assert SCOPE_MARKER in reply, "reply dig pertama harus bertanda SCOPE_MARKER"
    assert reply.strip() != _ask_reply(
        extract_program_meta().get("judul", ""), units
    ).strip(), "reply dig masih teks deterministik _ask_reply (bukan LLM)!"
    assert "?" in reply, f"reply dig seharusnya menggali (ada pertanyaan): {reply[:120]!r}"
    print(f"      Dig LLM: {reply[:90].replace(chr(10), ' ')}...")

    # c) Toleransi: prompt dig tahan program draft (units kosong)
    upd2 = agent1_node({"chat_history": [{"role": "user", "content": "halo"}], "modules": []})
    assert upd2["chat_history"], "dig draft gagal total"


step("8c. Auto-effort + dig percakapan LLM (ronde 3)", t_dig)


# --- 9. Inject demo --------------------------------------------------------
def t_inject():
    from agents.state import ModuleState
    from tools.word_injector import inject_module

    demo = ModuleState(
        module_id="DEMO",
        module_title="Demo Render Template",
        syllabus_content="| Elemen | KUK | JP |",
        draft_json={"judul_modul": "Demo Render Template"},  # minimal 1 bagian terisi
        evaluator_feedback="",
        status_evaluasi="PASS",
        iteration_count=1,
    )
    path = inject_module(demo, output_dir=None)
    assert path.exists(), f"file hasil tidak ada: {path}"
    print(f"      File demo (default): {path}")

    # Bukti injeksi NYATA: draft berisi konten -> teks muncul di docx hasil.
    filled = ModuleState(
        **{**demo, "module_id": "DEMOfill",
           "draft_json": {"judul_modul": "MODUL UJI INJEKSI 12345",
                          "elemen_rows": [{"elemen": "E1", "kuk": "K1", "pengetahuan": "P1", "keterampilan": "S1"}]}}
    )
    path2 = inject_module(filled, output_dir=None)
    from tools.doc_utils import extract_docx_text

    text = extract_docx_text(path2)
    # Ronde 11: judul_modul ada di TEXTBOX sampul template resmi 2024 ->
    # tidak terbaca doc.paragraphs; cek langsung raw XML dokumen.
    import zipfile as _zip

    _xml = _zip.ZipFile(str(path2)).read("word/document.xml").decode("utf-8", "replace")
    assert "MODUL UJI INJEKSI 12345" in _xml, "judul_modul tidak ter-inject ke docx!"
    assert "E1" in text, "loop elemen_rows tidak ter-render di tabel silabus!"
    print(f"      File demo (isi nyata): {path2} - injeksi & loop tabel OK")


step("9. Demo inject docxtpl", t_inject)


# --- 10. FULL: Agent 3 structured output + mini pipeline -------------------
if "--full" in sys.argv:
    def t_full():
        # a) Agent 3 node nyata (structured output + fallback) terhadap draf kecil
        from agents.agent3_evaluator import agent3_node
        from agents.state import make_module

        fake = make_module("T01", "Uji Evaluasi", "| Elemen | KUK | JP |")
        fake["draft_json"] = {
            "kata_pengantar": ("Pengantar modul ini membahas pentingnya kompetensi.\n\n"
                               "Paragraf kedua menjelaskan ruang lingkup.\n\n"
                               "Paragraf ketiga menutup pengantar."),
            "pengetahuan_content": ("Materi teori detail multi-paragraf tentang langkah kerja.\n\n"
                                    "```mermaid\nflowchart TD\nA[Mulai] --> B[Siapkan Alat] --> C[Kerjakan] --> D[Selesai]\n```\n"),
        }
        upd = agent3_node(fake)["modules"][0]
        print(f"      Verdict agent3_node: {upd['status_evaluasi']} (iter {upd['iteration_count']})")
        assert upd["status_evaluasi"] in ("PASS", "FAIL")

        # b) mini pipeline 1 modul end-to-end (tanpa HITL approval manual).
        #    Agent 1 dua tahap (dig dulu, baru build): seed riwayat langsung
        #    berisi tanya-cakupan + jawaban user agar build langsung jalan.
        import uuid

        from agents.agent1_syllabus import SCOPE_MARKER
        from main_graph import build_graph

        cfg = {"configurable": {"thread_id": "smoke-" + str(uuid.uuid4())}}
        graph = build_graph()
        init = {
            "chat_history": [
                {"role": "user", "content": "Buatkan modul pelatihan dari program PLTSa"},
                {"role": "assistant", "content": f"{SCOPE_MARKER}\nDaftar unit tersedia. Mau semua atau pilih?"},
                {"role": "user", "content": "Buat untuk unit 1.1 saja (Memproses Penerimaan Sampah)"},
            ],
            "program_name": "",
            "modules": [],
            "approved_by_human": False,
            "final_documents": [],
        }
        graph.invoke(init, cfg)
        snapshot = graph.get_state(cfg)
        assert "Map_Modules" in snapshot.next, f"graph tidak berhenti di HITL (next={snapshot.next})"
        mods = snapshot.values["modules"]
        assert len(mods) == 1, f"harus tepat 1 modul, dapat {len(mods)}"
        assert mods[0]["module_title"] == "Memproses Penerimaan Sampah", f"judul: {mods[0]['module_title']!r}"
        assert mods[0]["kode_unit"] == "E.38SPH02.014.01", f"kode: {mods[0]['kode_unit']!r}"
        assert mods[0]["syllabus_rows"], "syllabus_rows kosong (harus dari tabel acuan docx)"

        # Feedback ronde 2: pengetahuan bernomor urut + total JP = alokasi.
        import re as _re
        m0 = mods[0]
        pn = m0["syllabus_rows"][0]["pengetahuan"]
        assert _re.match(r"^(Penjelasan tentang:\n)?1\. ", pn), f"pengetahuan tak bernomor: {pn[:60]!r}"
        nums = _re.findall(r"(?m)^(\d+)\. ", pn)
        assert nums == [str(i + 1) for i in range(len(nums))], f"penomoran tak urut: {nums}"
        total_jp = sum(int(_re.search(r"(\d+)", r["durasi"]).group(1)) for r in m0["syllabus_rows"])
        alok_jp = int(_re.search(r"(\d+)", m0["alokasi_waktu"]).group(1))
        assert total_jp == alok_jp, f"total JP {total_jp} != alokasi {alok_jp}"
        print(f"      HITL OK - 1 modul: {m0['module_title']} | rows={len(m0['syllabus_rows'])}, "
              f"JP={total_jp}/{alok_jp}, pengetahuan bernomor, next={snapshot.next}")

        # approve + resume sampai END
        graph.update_state(cfg, {"approved_by_human": True})
        for _ in graph.stream(None, cfg, stream_mode="updates"):
            pass
        final = graph.get_state(cfg).values
        print(f"      Status modul: {[(m['module_id'], m['status_evaluasi'], m['iteration_count']) for m in final['modules']]}")
        print(f"      Dokumen akhir: {final['final_documents']}")
        assert final["final_documents"], "tidak ada file .docx yang dihasilkan"

        # Ronde 6: tabel elemen di docx final WAJIB terisi (loop {%tr %}).
        # Terukur: agent2 kadang tidak menyertakan elemen_rows -> tabel kosong
        # tanpa peringatan; kini ada fallback + asersi di sini.
        from docx import Document as _Doc

        _doc = _Doc(final["final_documents"][0])
        _elem_tbls = [
            _t for _t in _doc.tables
            if len(_t.rows) > 1 and "KRITERIA UNJUK KERJA" in _t.rows[0].cells[1].text.upper()
        ]
        assert _elem_tbls and len(_elem_tbls[0].rows) > 1, (
            "tabel elemen di docx final kosong - loop elemen_rows tidak ter-render"
        )
        print(f"      Tabel elemen terisi: {len(_elem_tbls[0].rows) - 1} baris data")

    step("10. FULL pipeline end-to-end (LLM nyata)", t_full)


print("\n" + "=" * 60)
if failures:
    print(f"HASIL: {failures} langkah GAGAL - perbaiki sebelum lanjut.")
    sys.exit(1)
print("HASIL: SEMUA LANGKAH LOLOS - sistem siap.")
sys.exit(0)