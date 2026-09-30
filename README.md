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
python -m rag.kb                 # ingest template + contoh modul ke Chroma (idempoten)
python -m rag.kb --force         # rebuild koleksi KB dari nol
python -m rag.kb "bagian wajib modul"      # tes retrieve KB saja
python tools/rag_verify.py       # BUKTI semua koleksi berisi vektor & bisa dicari
python tools/image_verify.py --selftest    # swap cover tak merusak entry lain (tanpa API)
python tools/image_verify.py --live        # 1 gambar Replicate sungguhan (berbayar)
python tools/image_verify.py --docx output/X.docx   # cek cover hasil render
python -m tools.word_injector    # demo render template (tanpa LLM)
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

KB referensi (`rag/kb.py`) — blok `# --- KB REFERENSI ---` di `.env.example`:
- `RAG_KB_COLLECTION` (default `template_kemnaker`), `RAG_TOP_K_KB` (slot di hasil gabungan).
- `RAG_KB_INGEST_ON_BOOT` (ingest otomatis saat server menyala), `RAG_KB_INCLUDE_PROGRAM`.

Generate gambar (`tools/image_gen.py`) — blok `# --- GENERATE GAMBAR ---`. Sejak ronde 20
gambar dibuat **dua tingkat** (dua model berbeda):
- `REPLICATE_API_TOKEN` — **tanpa ini semua gambar jatuh ke pencarian gambar internet.**
- `REPLICATE_MODEL` — model **subbab** (default `black-forest-labs/flux-schnell`).
- `REPLICATE_MODEL_COVER` — model **cover** (default `google/nano-banana-2`), dipanggil sekali per modul.
- `IMAGE_ASPECT` — rasio gambar subbab.
- `IMAGE_MODE` — `replicate` (Replicate dulu untuk setiap gambar) / `search` / `auto`.
- `IMAGE_COVER_MODE` — `replicate` (tukar foto cover template) / `template` (kill switch).
- `IMAGE_CACHE_DIR` — cache hasil generate (prompt sama tidak dibayar dua kali).

**Skema input berbeda per keluarga model — field milik model lain = HTTP 422.** `image_gen.py`
memilih payload dari **slug model yang dipanggil** (`_model_family()`); slug yang dikenal selalu
menang atas override, supaya satu nilai env tak bisa merusak model keluarga lain:

| Keluarga | Model | Field ukuran | Enum ukuran | Env |
|---|---|---|---|---|
| `flux` | `black-forest-labs/flux-schnell` | `megapixels` | `"1"` \| `"0.25"` (maks 1 MP) | `IMAGE_MEGAPIXELS` |
| `gemini` | `google/nano-banana-2` | `resolution` | `1K` \| `2K` \| `4K` | `IMAGE_RESOLUTION`, `IMAGE_COVER_RESOLUTION` |
| `generic` | apa pun | *(tidak ada)* | hanya `prompt`+`aspect_ratio`+`output_format` | — |

Mengirim `resolution` ke flux, atau `megapixels` ke nano-banana, dijawab **422**. Bila memakai
model di luar dua keluarga itu, paksa dengan `IMAGE_MODEL_FAMILY` / `IMAGE_MODEL_FAMILY_COVER`
(`auto|flux|gemini|generic`). Verifikasi **gratis tanpa kredit** (membaca skema kedua model dari
Replicate lalu mencocokkan tiap field payload): `python tools/image_verify.py --schema`.

`IMAGE_OUTPUT_QUALITY` (0–100) hanya berpengaruh untuk `jpg`/`webp`; `png` diabaikan.

`IMAGE_OUTPUT_FORMAT` (`png` default | `jpg`) menentukan format berkas gambar **subbab**; cover
selalu JPEG. `png` lossless tapi berat — satu modul dengan 21 gambar ≈ 10 MB, dan berkas sebesar
itu ikut terunduh tiap kali modul dibuka. `jpg` (quality 88 + optimize) menurunkannya ke ≈ 2 MB
tanpa beda kasat mata pada kotak template 135×100 mm. Format ikut menjadi kunci cache, jadi
mengganti nilai ini membuat cache lama tidak terpakai **sekali** (generate ulang = gambar berbayar).

`IMAGE_NET_RETRIES` (default `3`) mengulang panggilan Replicate yang gagal karena **jaringan**
(ConnectTimeout/ReadTimeout/handshake TLS), dengan jeda 2 s lalu 4 s. Kesalahan HTTP 4xx/5xx
sengaja **tidak** diulang — itu bug payload/model. Retry ini penting untuk mutu: satu
ConnectTimeout pernah membuat cover modul jatuh ke foto template lama, dan satu ReadTimeout
membuat gambar subbab jatuh ke foto internet — keduanya tanpa peringatan yang mencolok.

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
│   ├── image_gen.py        # Generate gambar + cover via Replicate
│   ├── image_search.py     # Pencarian gambar internet (jaring pengaman terakhir)
│   ├── word_injector.py    # docxtpl: draft_json → file .docx (+ tukar cover)
│   ├── rag_verify.py       # Bukti koleksi Chroma berisi vektor & bisa dicari
│   └── image_verify.py     # Bukti Replicate jalan & swap cover tak merusak docx
├── rag/                    # RAG chat (tanya dokumen) — Rewrite-Retrieve-Read
│   ├── chat.py             # Orkestrator jawaban streaming (dipakai server.py)
│   ├── retriever.py        # Retrieve gabungan (rag_chat + skkni + KB referensi)
│   ├── kb.py               # Ingest/retrieve KB referensi (template + contoh modul)
│   ├── rewriter.py         # Tulis-ulang pertanyaan (LLM)
│   ├── context.py          # create-and-refine / tree-summarization (overflow)
│   ├── document_manager.py # upload/list/delete + ingest program
│   ├── chunker.py          # Muat PDF/DOCX + potong chunk stabil
│   ├── store.py            # Abstraksi VectorStore (Chroma) — Qdrant nanti
│   ├── threads.py          # Riwayat percakapan RAG (in-memory)
│   └── smoke.py            # Smoke struktural + ingest/retrieve/delete
└── database/
    ├── skkni_docs/         # PDF SKKNI (sumber RAG)
    ├── contoh_modul/       # Contoh modul (acuan gaya Agent 2 + KB referensi)
    ├── chroma_db/          # Index vector persist (skkni + rag_chat + KB)
    └── template_kemnaker.docx
```

## Catatan desain

- **Map-Reduce:** `Map_Modules` membelah tiap modul via `Send("Agent2_Node", module)` — worker jalan paralel; hasil merge ke `GlobalState["modules"]` lewat reducer upsert `merge_modules`.
- **HITL:** `interrupt_before=["Map_Modules"]` — graph pause sampai UI set `approved_by_human=True`.
- **Loop revisi:** Agent 3 FAIL → `Send` balik ke Agent 2 dengan feedback; iterasi ke-3 dipaksa PASS "Need Human Review" (anti infinite-loop).
- **Tag template dinamis:** Agent 2 menerima daftar tag yang ditemukan otomatis dari template docx — ganti template pun tanpa ubah kode.

### Tiga korpus RAG (bukan satu)

| Koleksi | Isi | Sifat |
|---|---|---|
| `skkni_kemnaker` | PDF SKKNI (5953 chunk) | tetap, di-ship di image |
| `rag_chat` | dokumen unggahan user + program aktif | **mutable** (upload/hapus) |
| `template_kemnaker` | `template_kemnaker.docx` + `contoh_modul/` (151 chunk) | tetap, referensi |

`rag/retriever.py` menggabungkan ketiganya; `template` dan `contoh_modul` muncul sebagai
`sources` di jawaban sehingga acuan gaya/struktur ikut tersitasi. Karena korpus contoh modul
jauh lebih besar dan berprosa asli sementara template hanya 8 chunk ber-`{{ tag }}`, template
**selalu kalah skor** pada query umum — `rag/kb.py` karena itu menyisipkan satu slot template
yang dijamin ada (`ENSURE_SOURCE_TYPE`), supaya template benar-benar terpakai dan bukan hanya
"ada di Chroma tapi tak pernah terambil".

Idempotensi KB memakai `content_hash` (sha1 isi file), **bukan path** — path absolut Windows
berbeda dari container Linux, sehingga pengecekan berbasis path akan selalu menganggap dokumen
berubah dan me-re-embed tiap boot.

### Cover page = slot template yang ditukar, bukan gambar baru

Template Kemnaker sudah punya foto cover tertanam: `word/media/image3.jpg` (1400×980 px,
rasio **10:7**), dirujuk tepat sekali sebagai anchor `behindDoc` berukuran 205.9×144.1 mm di
belakang textbox judul. Cara lama (fetch foto lalu overlay) **ditolak reviewer** karena
menutupi gambar template. Ronde 18 menukar **byte** entry itu dengan ilustrasi Replicate:
posisi, ukuran, dan tata letak textbox tidak bergeser sama sekali, dan gambar dijamin terlihat
untuk semua panjang judul. Word menghormati extent XML, jadi rasio hasil **harus** persis 10:7
atau gambar akan gepeng — `generate_cover()` meminta 3:2 lalu memotong lokal ke 10:7.

Keutuhan dokumen dijaga `_swap_zip_entry()`: semua entry disalin dalam urutan asli memakai
`ZipInfo` aslinya (date_time/compress_type/external_attr ikut terjaga), `[Content_Types].xml`
tidak pernah ditulis ulang. `python tools/image_verify.py --selftest` membuktikan hanya entry
cover yang berubah — tanpa perlu kredit Replicate.

Rantai fallback gambar tidak boleh putus: Replicate → pencarian gambar internet → dan bila
keduanya gagal, **cover template dibiarkan apa adanya**. Injeksi `.docx` tidak pernah gagal
karena generator gambar.

**Model gambar, dua tingkat (ronde 20).** Gambar subbab dibuat `black-forest-labs/flux-schnell`
(≈$0.003/gambar, ~11× lebih murah dari `google/nano-banana-2`). Pertimbangannya bukan hanya harga:
gaya gambar di modul ini menuntut *wordless artwork* (teks yang muncul di ilustrasi justru pernah
jadi masalah), jadi keunggulan utama nano-banana — rendering teks yang akurat — memang sengaja
tidak dipakai di sana. **Cover justru sebaliknya**, dan itu satu-satunya gambar yang memakai
nano-banana: cover dicetak pada 205,9 mm dan hanya dibuat **sekali per modul**, sementara
`flux-schnell` mentok di 1 MP (`megapixels` maks `"1"`) sehingga cover 10:7-nya hanya ≈1189×832 px
— **lebih lunak daripada foto template yang digantikannya** (1400×980 px ≈ 173 dpi). Pada
`IMAGE_COVER_RESOLUTION=2K`, nano-banana memberi 2528×1696 px yang di-crop ke **2423×1696 px**
(≈299 dpi pada 205,9 mm), jadi cover hasil generate justru **lebih tajam** dari template. Biaya naik dari ≈$0.018 menjadi ≈$0.054 per modul (6 gambar) —
masih jauh di bawah satu modul nano-banana penuh (≈$0.23). Verifikasi gratis tanpa kredit:
`image_verify.py --schema`.