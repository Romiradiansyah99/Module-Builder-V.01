# DEPLOY.md — Kemnaker Module Builder di VPS (Docker)

Panduan deployment tim: VPS cloud Linux + Docker, LLM via API CommandCode
(endpoint OpenAI-compatible, API key, tanpa runtime LLM lokal), login
**kata sandi tunggal**.

---

## 1. Provision VPS

- Ubuntu 22.04/24.04, **2 vCPU / 4GB RAM** cukup (LLM remote; beban lokal
  hanya chromadb + onnxruntime + FastAPI). Disk ≥ 5GB.
- Install Docker:

  ```bash
  curl -fsSL https://get.docker.com | sh
  # atau: apt install docker.io docker-compose-plugin
  ```

- Firewall:

  ```bash
  ufw allow OpenSSH
  ufw allow 80/tcp && ufw allow 443/tcp
  ufw enable
  ```

## 2. Kirim proyek ke VPS

Dari mesin lokal (Git Bash / WSL / rsync):

```bash
rsync -av --exclude .venv --exclude __pycache__ \
  --exclude output --exclude runtime --exclude database/uploads \
  --exclude .env --exclude "*.log" \
  ./ vps:/opt/kemnaker/
```

> Build context ±70MB (termasuk `database/chroma_db/` 44,7MB) — sekali saja.
> Koneksi lambat: build lokal lalu `docker save | ssh docker load`.

## 3. Siapkan `.env` di VPS

```bash
cp .env.example .env && chmod 600 .env
openssl rand -hex 32      # -> SESSION_SECRET
python3 -c "import hashlib;print(hashlib.sha256(b'SANDI_TIM_ANDA').hexdigest())"
                          # -> APP_PASSWORD_SHA256
nano .env
```

Yang **wajib** diisi/dicek di `.env` VPS:

| Variabel | Nilai | Catatan |
|---|---|---|
| `LLM_BASE_URL` | `https://api.commandcode.ai/provider/v1` | endpoint OpenAI-compatible CommandCode (`/chat/completions`). Host non-Ollama otomatis memakai mode ini |
| `LLM_API_KEY` | **key BARU (dirotasi)** | key lama pernah plaintext di repo lokal — jangan dipakai ulang |
| `LLM_MODEL` | `deepseek/deepseek-v4.1-flash` | nama model persis seperti yang dikenali CommandCode. Model tak dikenal menjawab `HTTP 4xx` → dig Agent 1 mati senyap (fallback deterministik). Uji dengan panggilan nyata (lihat bawah) |
| `LLM_MODEL_FAST` | `deepseek/deepseek-v4.1-flash` | model chat ringan (effort "low", dig Agent 1) |
| `EMBEDDING_MODEL` | **KOSONG** | ❗ wajib kosong — index chroma_db dibangun dengan MiniLM lokal; embedder remote = RAG rusak senyap |
| `REPLICATE_API_TOKEN` | token dari replicate.com | ❗ tanpa ini **semua gambar** jatuh ke pencarian gambar internet dan cover tetap foto template |
| `REPLICATE_MODEL` | `black-forest-labs/flux-schnell` | model **subbab** (banyak gambar/modul) — ~$0.003/gambar, ~11× lebih murah dari nano-banana. **Skema input BEDA per keluarga** — lihat catatan di bawah |
| `REPLICATE_MODEL_COVER` | `google/nano-banana-2` | model **cover** (1×/modul). flux mentok 1 MP → cover 10:7 hanya ≈1189×832 px, lebih lunak dari template 1400×980 px |
| `IMAGE_COVER_RESOLUTION` | `2K` | resolusi cover → terukur 2423×1696 px (≈299 dpi pada 205,9 mm), lebih tajam dari template |
| `APP_PASSWORD_SHA256` | hash SHA-256 sandi tim | |
| `SESSION_SECRET` | `openssl rand -hex 32` | |
| `COOKIE_SECURE` | `1` setelah HTTPS aktif | |

Cek cepat bahwa **model LLM** benar-benar hidup — uji dengan panggilan nyata.
Ganti `MODEL` dengan `LLM_MODEL` di `.env`; `HTTP 200` = aman, `4xx` = model/key
salah (periksa nama model & `LLM_API_KEY`):

```bash
curl -s -o /dev/null -w "%{http_code}\n" "$LLM_BASE_URL/chat/completions" \
  -H "Authorization: Bearer $LLM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"MODEL","messages":[{"role":"user","content":"ok"}],"stream":false}'
```

Cek cepat bahwa token Replicate benar-benar hidup (1 gambar berbayar):

```bash
docker compose exec app python tools/image_verify.py --live
```

**Skema input berbeda per keluarga model — jangan campur field.** `tools/image_gen.py` memilih
payload dari slug model yang dipanggil, jadi mengganti model tidak perlu ubah kode:

| Keluarga | Field ukuran | Enum | Env |
|---|---|---|---|
| `flux` (flux-schnell) | `megapixels` | `"1"`, `"0.25"` | `IMAGE_MEGAPIXELS` |
| `gemini` (nano-banana-2) | `resolution` | `1K`, `2K`, `4K` | `IMAGE_RESOLUTION`, `IMAGE_COVER_RESOLUTION` |
| `generic` | *(tidak ada)* | — | `IMAGE_MODEL_FAMILY[_COVER]=generic` |

Mengirim `resolution` ke flux, atau `megapixels` ke nano-banana, dijawab **HTTP 422**.
Keluarga dideteksi dari slug dan slug yang dikenal **selalu menang** atas override; paksa dengan
`IMAGE_MODEL_FAMILY` / `IMAGE_MODEL_FAMILY_COVER` hanya bila modelnya di luar yang dikenal.

Verifikasi **gratis tanpa kredit** (baca skema model dari Replicate, lalu cek setiap field
payload ada di dalamnya — inilah gerbang yang dulu tidak ada sehingga bug field `size`
tersembunyi 5 ronde):

```bash
docker compose exec app python tools/image_verify.py --schema
```

**Dua tingkat itu disengaja.** Subbab = flux-schnell (murah, dipanggil berkali-kali). Cover =
nano-banana-2 pada `IMAGE_COVER_RESOLUTION=2K` (dipanggil **sekali** per modul) karena
flux-schnell dibatasi **1 MP** — cover 10:7-nya hanya ≈1189×832 px (≈147 dpi pada lebar
205,9 mm), **lebih lunak** daripada foto template yang digantikannya (1400×980 px ≈ 173 dpi).
Dengan 2K, cover jadi 2528×1696 px yang di-crop ke 2423×1696 px (≈299 dpi) — lebih tajam dari
template. Biaya ≈$0.054/modul
(6 gambar) versus ≈$0.23 bila seluruh modul memakai nano-banana. Bila ingin menyederhanakan,
isi `REPLICATE_MODEL_COVER` dengan slug yang sama dengan `REPLICATE_MODEL` — sah, tapi cover
akan lebih lunak dari template.

## 3b. Ingest KB referensi SEBELUM build

`database/chroma_db/` **ikut di-ship di dalam image** (`Dockerfile` sengaja tidak
mengecualikannya), dan **bukan volume** — satu-satunya volume adalah `./runtime:/app/runtime`.
Artinya isi `chroma_db` di dalam image adalah data imutabel, dan **apa pun yang ditulis ke
sana saat kontainer berjalan akan HILANG** begitu kontainer di-recreate.

Jadi koleksi KB harus sudah terisi **di build context, sebelum** `docker compose build`.
Lakukan **secara lokal** lalu rsync (langkah 2 memang mengikutkan `database/chroma_db/`):

```bash
python -m rag.kb            # idempoten: run kedua = 0 embed, 3 skip
python tools/rag_verify.py  # gerbang: semua koleksi harus 384-d & bisa dicari
rsync ...                   # langkah 2
# di VPS:
docker compose build && docker compose up -d
```

❗ **Jangan** mengandalkan `docker compose exec app python -m rag.kb` untuk menjadikan KB
permanen. Perintah itu menulis ke lapisan kontainer yang akan dibuang pada recreate berikutnya,
dan gejalanya menipu: KB terlihat benar sampai kontainer di-recreate, lalu kembali ke versi
image tanpa pesan error. `RAG_KB_INGEST_ON_BOOT=1` (default) memang menjalankan ingest saat
boot, tetapi hasilnya **no-op** selama `content_hash` cocok; bila ia benar-benar meng-embed
ulang, server mencetak peringatan eksplisit:

```text
[server] !! KB di-index ULANG saat boot (N dokumen) -> hanya bertahan di kontainer ini.
```

Konsekuensinya: rebuild image **wajib** setiap kali `database/contoh_modul/` atau
`template_kemnaker.docx` berubah — index di dalam image tidak ikut menua sendiri.

## 4. Jalankan

```bash
docker compose build
docker compose up -d
docker compose logs -f        # tunggu: [server] RAG SKKNI siap. + [rag.kb] KB referensi siap:
curl -fsS localhost:8000/healthz
# Uji konektivitas LLM sekali dari dalam kontainer:
docker compose exec app python smoke_test.py
# Bukti ketiga korpus RAG berisi vektor (read-only):
docker compose exec app python tools/rag_verify.py
```

Di `/healthz`: `rag_ready=true` **dan** `kb.error=null`. `KB_INGEST_ON_BOOT` sengaja tidak
pernah menulis `_rag_error`, sehingga kegagalan KB **tidak bisa** mematikan `/api/chat`.

Buka `http://IP_VPS:8000` → diarahkan ke `/login` → masuk dengan sandi tim.

## 5. HTTPS via Caddy (disarankan)

Tambahkan di `docker-compose.yml`:

```yaml
  caddy:
    image: caddy:2
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    volumes:
      - ./deploy/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
volumes:
  caddy_data:
```

`deploy/Caddyfile`:

```
domain-anda.com {
    reverse_proxy app:8000
    flush_interval -1    # jaga SSE tetap mengalir
}
```

Point DNS A record ke IP VPS → `docker compose up -d` → Let's Encrypt
otomatis. Lalu set `COOKIE_SECURE=1` di `.env` dan `docker compose up -d`
lagi. (Tanpa domain: pakai HTTP `8000:8000` — cukup untuk tim internal.)

### 5b. Tanpa domain — Cloudflare Tunnel `trycloudflare` (gratis)

Tidak perlu Caddy maupun DNS. App tetap bind `127.0.0.1:8000` (kompose sudah
begitu); hanya tunnel yang menyentuhnya. HTTPS diurus Cloudflare.

```bash
# Install tunnel sekali:
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

# Jalankan quick tunnel (URL acak, hidup selama proses ini jalan):
cloudflared tunnel --url http://localhost:8000
# -> baca https://<random>.trycloudflare.com dari output, buka di browser.
```

Catatan:
- URL acak berubah tiap kali `cloudflared` dijalankan ulang. Untuk URL tetap
  dibutuhkan **named tunnel** + domain (lihat §5).
- `COOKIE_SECURE` biarkan `0` (tunnel trycloudflare HTTP di sisi Cloudflare,
  cookie tetap lewat HTTPS publik — cukup; set `1` bila pakai domain §5).
- Wajib pasang `APP_PASSWORD_SHA256` + `SESSION_SECRET` sebelum diekspos:
  tanpa itu siapa pun bisa login & memicu pipeline LLM berbayar.
- Rotasi `LLM_API_KEY` dulu — key lama pernah plaintext di repo lokal.

### 5c. Jalankan tunnel sebagai systemd agar tahan restart (disarankan)

Menjalankan `cloudflared tunnel` manual di SSH akan mati begitu sesi SSH
ditutup (bahkan dengan `nohup`). Untuk quick tunnel yang bertahan, gunakan
**systemd service** (di VPS yang PID1-nya systemd):

```bash
cat > /etc/systemd/system/kemnaker-tunnel.service <<'UNIT'
[Unit]
Description=Cloudflare tunnel -> Kemnaker app (port 8010)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/cloudflared tunnel --url http://127.0.0.1:8010 --no-autoupdate
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now kemnaker-tunnel
systemctl is-active kemnaker-tunnel          # -> active
# Ambil URL acak dari log:
journalctl -u kemnaker-tunnel --no-pager | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1
```

Sesuaikan `--url` ke port host tempat app terbuka (mis. `http://127.0.0.1:8010`
bila `docker-compose.yml` memetakan `8010:8000`). Restart app tidak mematikan
tunnel service — keduanya independen.

## 6. Operasional harian

| Tugas | Perintah |
|---|---|
| Update kode | `rsync` proyek → `docker compose build && docker compose up -d` |
| Ganti sandi tim | edit `APP_PASSWORD_SHA256` di `.env` → `docker compose up -d` (sesi pengguna lain selamat) |
| Log | `docker compose logs -f app` |
| Restart | `docker compose restart` |
| Backup (cron malam) | `tar czf /backup/backup-$(date +\%F).tgz /opt/kemnaker/runtime` |
| Bersihkan uploads lama | `rm /opt/kemnaker/runtime/uploads/*.docx` secara berkala (manual) |

Semua state yang selamat restart ada di `/opt/kemnaker/runtime/`:
`uploads/`, `output/<thread_id>/*.docx`, `checkpoints.sqlite`,
`sessions.json`, `ai_memory/<owner>.json`, `home/.cache/chroma/` (cache ONNX).

## 7. Perilaku yang disepakati (privat per-browser)

- **Riwayat percakapan privat per-perangkat.** Satu sandi tim untuk masuk,
  tetapi setiap browser punya **identitas sendiri** (`kb_owner` — cookie
  bertanda tangan, HttpOnly, ~400 hari). Sidebar hanya menampilkan thread
  milik browser itu; thread orang lain menjawab **404** (bukan 403) supaya
  keberadaannya tidak bisa ditebak.
- **Keluar bukan menghapus.** Tombol "Keluar →" hanya menghapus sesi login
  (`kb_session`). Identitas (`kb_owner`) sengaja dipertahankan, sehingga
  **login lagi di browser yang sama memunculkan kembali riwayat**. Berganti
  perangkat / mode privat = identitas baru = riwayat kosong.
- **`ai_memory` per-pemilik**: aturan sikap disimpan di
  `runtime/ai_memory/<owner>.json`, jadi kebiasaan satu orang tidak bocor ke
  rekan setim. Statistik `llm_usage` tetap organisasi (dipakai bersama).
- **Masih bersama tim** (memang tidak diprivatkan): daftar unit
  (`/api/units`), dokumen RAG (`/api/rag/docs`) beserta upload/hapusnya,
  program aktif default (`active_program`), dan `llm_usage` di `/api/info`.
- Maksimal **2 produksi paralel** (`PRODUCE_CONCURRENCY`); permintaan ke-3
  menerima pesan "coba lagi beberapa saat".
- **Jangan pernah** menaikkan `--workers` di atas 1 — SqliteSaver tidak
  multi-process safe (checkpoint korup, modul dobel).
- **Jangan pernah** merotasi `SESSION_SECRET`: itu mengeluarkan semua orang
  sekaligus dan **mengorbitkan setiap identitas** (riwayat semua browser
  mendadak tak terlihat, walau filenya masih ada).
- File sesi & checkpoint hilang hanya jika volume `./runtime/` dihapus.

## 8. Pemecahan masalah cepat

| Gejala | Sebab umum | Solusi |
|---|---|---|
| RAG balasan tidak relevan tapi tak ada error | `EMBEDDING_MODEL` terisi remote di VPS | kosongkan `EMBEDDING_MODEL=`, `docker compose up -d` |
| `/healthz` gagal di healthcheck | ingest RAG pertama masih berjalan | tunggu (start-period 90s); cek `docker compose logs` |
| Login kembali setiap saat | `SESSION_SECRET` berubah tiap restart | pastikan SESSION_SECRET tetap di `.env` |
| SSE terasa macet di depan proxy | buffering | Caddy: `flush_interval -1` |
| Key ditolak (401 dari `api.commandcode.ai`) | key salah/dirotasi/mati | perbarui `LLM_API_KEY` di `.env`, `docker compose up -d` |
| Dig Agent 1 balas template kaku, log `4xx` | `LLM_MODEL` tidak dikenal endpoint | uji dengan panggilan nyata di atas, lalu set `LLM_MODEL` ke model yang menjawab **200** |
| RAG chat/typing diam, log `404 /api/chat` | mode provider salah terdeteksi | pastikan `LLM_BASE_URL` menunjuk host non-Ollama (mis. `api.commandcode.ai`) lalu `docker compose up -d` |
| **Gambar modul bukan hasil AI** (foto internet / ber-watermark) | `REPLICATE_API_TOKEN` kosong, atau `IMAGE_MODE` bukan `replicate`, atau kredit habis | cek log `[image_gen] Replicate submit gagal (...)`; `docker compose exec app python tools/image_verify.py --live` |
| Cover masih foto template | `IMAGE_COVER_MODE=template`, kredit habis, atau gangguan jaringan sesaat saat submit cover | pastikan `IMAGE_COVER_MODE=replicate` + kredit cukup. Gangguan jaringan (log: `gangguan jaringan (ConnectTimeout) - ulangi 2/3`) kini diulang otomatis `IMAGE_NET_RETRIES` (default 3); kalau log berakhir `Cover Replicate tidak tersedia`, generate-nya memang gagal total. Verifikasi hasil: `image_verify.py --docx output/X.docx` |
| Satu-dua gambar subbab jadi foto internet/ber-watermark | Replicate gagal setelah semua retry, jatuh ke jaring pengaman pencarian gambar (log: `Gambar internet OK (wikimedia)`) | lihat baris log tepat sebelum itu untuk sebabnya (`HTTP 402` = kredit, `HTTP 429` = rate limit, `gangguan jaringan` = jaringan). Naikkan `IMAGE_NET_RETRIES` bila jaringannya memang buruk |
| Satu subbab/subb-subbab **sama sekali** tanpa gambar di dokumen | Replicate **dan** pencarian internet gagal untuk query itu — slotnya sengaja dibiarkan kosong (injeksi `.docx` tidak boleh gagal karena generator gambar) | cari baris `[word_injector] PERINGATAN: tidak ada gambar utk "<query>"` di log — teks query di situ menunjuk slot yang kosong. Sebabnya ada di baris `[image_gen] Replicate submit gagal`/`gangguan jaringan` tepat di atasnya (kredit, rate limit, atau jaringan). Panggilan yang gagal **tidak** diulang di produksi berikutnya dari cache (cache hanya menyimpan hasil sukses) |
| Log `HTTP 402 Insufficient credit` | kredit Replicate habis | isi di https://replicate.com/account/billing lalu tunggu beberapa menit |
| Log `HTTP 422` di `[image_gen] Replicate submit gagal` | payload memuat field milik keluarga model lain (`resolution` ke flux / `megapixels` ke nano-banana) | jalankan `image_verify.py --schema`; set `IMAGE_MODEL_FAMILY` sesuai model, atau kosongkan ke `auto` |
| Log `HTTP 404` | slug `REPLICATE_MODEL` salah (mis. `google/nano-banana-v2` — model tidak ada) | cocokkan dengan slug di replicate.com; `image_verify.py --schema` mencetaknya |
| Log `HTTP 429` | rate limit (kredit < $5 → 6 req/menit) | turunkan `PRODUCE_CONCURRENCY` ke `1` untuk batch yang banyak gambarnya |
| Log `HTTP 429 ... rate limit ... less than $5.0 in credit` | kredit < $5 → dibatasi 6 prediksi/menit | normal; naikkan kredit atau turunkan `PRODUCE_CONCURRENCY=1` |
| Gambar bertambah banyak & biaya Replicate naik | ronde 18 menambah gambar per **sub-subbab** (alat/APD) | `IMAGE_SUBBAB_ENABLED=0` → kembali satu gambar per subbab elemen. Hasil generate di-cache di `IMAGE_CACHE_DIR`, jadi approve-ulang tidak membayar dua kali |
| Berkas `.docx` hasil unduhan berat (~10 MB) | 21 gambar subbab disimpan sebagai PNG (lossless) | `IMAGE_OUTPUT_FORMAT=jpg` → `docker compose up -d`; dokumen turun ke ~2 MB (ilustrasi di kotak 135×100 mm, mutu tak kasat mata). Cache lama tidak terpakai sekali — lihat baris berikut |
| Nomor daftar tampil ganda (`1. 1. Melaksanakan…`) | seharusnya tidak terjadi lagi: normalizer ronde 18 membuang prefiks ordinal dari paragraf ber-`numPr` saat render | jalankan `docker compose exec app python tools/docx_invariants.py output/X.docx` — bila masih ada, laporkan (normalizer gagal jalan: cek log `[word_injector] Normalisasi:`) |
| Baris daftar hanya baris pertama bernomor | sama seperti di atas — paragraf ber-`numPr` yang memuat `<w:br/>` dipecah per baris | idem: `tools/docx_invariants.py` |
| Nomor daftar / teks masih biru | template lama masih terpakai, atau normalizer tidak jalan | bangun ulang template: `python tools/template_builder.py` (target: 0 `00B0F0` di `document.xml` **dan** `numbering.xml`), lalu cek `tools/docx_invariants.py` |
| Tombol Approve menolak: "Gerbang penyusun belum lengkap" | Nama + jabatan/profesi penyusun belum diisi di kartu approval | isi kedua kolom bertanda `*` (NIP opsional). Gerbang yang sama juga dipasang di `main_graph.route_from_map`, jadi jalur Streamlit pun tidak bisa memproduksi modul tanpa penyusun |
| Pertanyaan soal struktur/gaya modul tak pernah menyitasi `template` | KB belum ter-ingest | `docker compose exec app python tools/rag_verify.py` → bila collection hilang: `python -m rag.kb` **lalu rebuild image** (index di-ship di dalam image) |
| `[rag.kb] PERINGATAN: embedder index (...) != embedder aktif (...)` | `EMBEDDING_MODEL` sempat terisi lalu dikosongkan | ingest sengaja **dilewati** agar index 384-d tidak rusak; `python -m rag.kb --force` untuk rebuild |