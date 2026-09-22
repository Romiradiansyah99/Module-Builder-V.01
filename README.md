# Kemnaker AI Module Builder

Agentic AI Module Generator modul pelatihan Kemnaker — LangGraph (Map-Reduce) + RAG SKKNI + docxtpl.

**Ganti/bikin ulang frontend?** Baca [`docs/FRONTEND_GUIDE.md`](docs/FRONTEND_GUIDE.md) — kontrak lengkap REST + SSE backend (`server.py`) untuk membangun UI baru tanpa menyentuh pipeline.

## Menjalankan

```bash
cd kemnaker-module-builder
pip install -r requirements.txt   # sekali saja
python server.py                  # dashboard demo -> http://localhost:8000
```

UI alternatif (fallback): `streamlit run app.py` → http://localhost:8501

Alur dashboard:
1. Chat kebutuhan pelatihan → Agent 1 (RAG SKKNI) menyusun silabus.
2. Review tabel silabus → **Approve** (produksi) atau **Revisi** (chat ulang).
3. Pipeline live: fan-out paralel per modul (kartu worker), Agent 2 (writer) → Agent 3 (QA, maks 3 iterasi) → inject `.docx`.
4. Unduh hasil di galeri akhir (folder `output/`).

## Fitur Tanya Dokumen (RAG chat)

Mode **Tanya Dokumen** (tombol di pojok kiri-atas dashboard) menjawab pertanyaan
atas dokumen lewat pola *Rewrite → Retrieve → Read*: menyusun ulang pertanyaan
(LLM), retrieve dari koleksi gabungan (dokumen Anda + program aktif + SKKNI),
lalu menjawab dengan **sitasi sumber** `[nama file, hal. n]` secara streaming.

- Unggah **PDF/DOCX** (tombol lampiran) → di-chunk → di-embed → masuk koleksi
  `rag_chat` (Chroma), dapat dihapus per dokumen dari panel *Tanya Dokumen* di sidebar.
- Program pelatihan aktif otomatis di-index saat server menyala.
- Konteks berlebihan ditangani strategi **create-and-refine** atau
  **tree-summarization** (atur `RAG_CONTEXT_STRATEGY`).
- Endpoint baru (semua dilindungi `require_auth`, tanpa mengubah pipeline modul):
  `POST /api/rag/chat/stream` · `POST /api/rag/stop/{id}` ·
  `POST /api/rag/docs/upload` · `GET /api/rag/docs` · `DELETE /api/rag/docs/{id}`

## Script pendukung

```bash
python smoke_test.py             # validasi semua komponen (ingest RAG otomatis)
python smoke_test.py --full     # + pipeline end-to-end 1 modul dengan LLM nyata
python -m rag.smoke             # smoke struktural RAG chat (chunker + store)
python -m rag.smoke --embed     # + ingest/retrieve/delete nyata (embedder lokal)
python tools/vector_store.py "query"       # tes RAG retrieve manual
python tools/vector_store.py "query" --force  # rebuild index dari nol
python tools/word_injector.py    # demo render template (tanpa LLM)
python main_graph.py             # cetak topologi graph ke konsol
```

## Konfigurasi

Semua endpoint diatur di `.env` (salin dari `.env.example`) — provider bisa berganti kapan saja tanpa ubah kode. **`.env` berisi API key dan tidak pernah di-commit.**
- `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` — LLM untuk Agent 1–3 (ChatOllama).
- `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` — embedding RAG (boleh beberapa kandidat dipisah koma; otomatis pilih yang jalan).
- `PROGRAM_DOC_PATH` — dokumen program pelatihan (konteks Agent 1).
- `EXAMPLE_MODULES_DIR` — folder contoh modul (acuan gaya Agent 2).
- `TEMPLATE_PATH` — template `.docx` dengan tag `{{ placeholder }}`.

RAG chat (`rag/`) — blok `# --- RAG CHAT ---` di `.env.example`:
- `RAG_CHAT_COLLECTION` (koleksi Chroma), `RAG_TOP_K_CHAT` (jumlah dokumen).
- `RAG_CONTEXT_STRATEGY` (`create-and-refine` / `tree-summarization`) & `RAG_CONTEXT_MAX_CHARS`.
- `RAG_HISTORY_MAX_TURNS` / `RAG_HISTORY_MAX_CHARS` — klip riwayat percakapan.
- `RAG_ANSWER_TEMPERATURE` / `RAG_ANSWER_MAX_TOKENS` / `RAG_REFINE_MAX_TOKENS`.
- `RAG_PROGRAM_DOC_PATH` — program docx yang otomatis di-index untuk RAG chat.

## Struktur

```text
├── server.py               # FastAPI backend: SSE streaming di sekeliling graph (dashboard demo)
├── static/index.html       # Dashboard single-page (tanpa build step)
├── app.py                  # UI Streamlit fallback: chat + approval + download
├── main_graph.py           # Routing LangGraph (Send API fan-out + interrupt HITL)
├── agents/
│   ├── state.py            # GlobalState & ModuleState (reducer upsert by module_id)
│   ├── agent1_syllabus.py  # Chat → RAG SKKNI → silabus JSON
│   ├── agent2_content.py   # Writer: silabus → draft_json (untuk docxtpl)
│   └── agent3_evaluator.py # QA strict (.with_structured_output, max 3 iterasi)
├── tools/
│   ├── llm_config.py       # Factory ChatOllama (endpoint via .env)
│   ├── vector_store.py     # RAG ChromaDB + ingest PDF SKKNI
│   ├── doc_utils.py        # Ekstraksi teks docx + penemuan tag template
│   ├── template_builder.py # Bangun ulang template docx dengan tag {{ placeholder }}
│   └── word_injector.py    # docxtpl: draft_json → file .docx
├── rag/                    # RAG chat (tanya dokumen) — Rewrite-Retrieve-Read
│   ├── chat.py             # Orkestrator jawaban streaming (dipakai server.py)
│   ├── retriever.py        # Retrieve gabungan (rag_chat + skkni_kemnaker)
│   ├── rewriter.py         # Tulis-ulang pertanyaan (LLM)
│   ├── context.py          # create-and-refine / tree-summarization (overflow)
│   ├── document_manager.py # upload/list/delete + ingest program
│   ├── chunker.py          # Muat PDF/DOCX + potong chunk stabil
│   ├── store.py            # Abstraksi VectorStore (Chroma) — Qdrant nanti
│   ├── threads.py          # Riwayat percakapan RAG (in-memory)
│   └── smoke.py            # Smoke struktural + ingest/retrieve/delete
└── database/
    ├── skkni_docs/         # PDF SKKNI (sumber RAG)
    ├── chroma_db/          # Index vector persist
    └── template_kemnaker.docx
```

## Catatan desain

- **Map-Reduce:** `Map_Modules` membelah tiap modul via `Send("Agent2_Node", module)` — worker jalan paralel; hasil merge ke `GlobalState["modules"]` lewat reducer upsert `merge_modules`.
- **HITL:** `interrupt_before=["Map_Modules"]` — graph pause sampai UI set `approved_by_human=True`.
- **Loop revisi:** Agent 3 FAIL → `Send` balik ke Agent 2 dengan feedback; iterasi ke-3 dipaksa PASS "Need Human Review" (anti infinite-loop).
- **Tag template dinamis:** Agent 2 menerima daftar tag yang ditemukan otomatis dari template docx — ganti template pun tanpa ubah kode.