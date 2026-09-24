# DEPLOY.md — Kemnaker Module Builder di VPS (Docker)

Panduan deployment tim: VPS cloud Linux + Docker, LLM via API ollama.com
(API key, tanpa Ollama desktop), login **kata sandi tunggal**.

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
| `LLM_BASE_URL` | `https://ollama.com` | API ollama.com langsung |
| `LLM_API_KEY` | **key BARU (dirotasi)** | key lama pernah plaintext di repo lokal — jangan dipakai ulang |
| `EMBEDDING_MODEL` | **KOSONG** | ❗ wajib kosong — index chroma_db dibangun dengan MiniLM lokal; embedder remote = RAG rusak senyap |
| `APP_PASSWORD_SHA256` | hash SHA-256 sandi tim | |
| `SESSION_SECRET` | `openssl rand -hex 32` | |
| `COOKIE_SECURE` | `1` setelah HTTPS aktif | |

## 4. Jalankan

```bash
docker compose build
docker compose up -d
docker compose logs -f        # tunggu: [server] RAG SKKNI siap.
curl -fsS localhost:8000/healthz
# Uji konektivitas LLM sekali dari dalam kontainer:
docker compose exec app python smoke_test.py
```

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
`sessions.json`, `ai_memory.json`, `home/.cache/chroma/` (cache ONNX).

## 7. Perilaku yang disepakati (dengan kata sandi tunggal)

- Siapa pun yang login bisa membuka thread URL dan mengunduh output siapa
  pun (satu sandi = satu ruang kerja tim).
- `ai_memory.json` (aturan sikap AI) dan statistik `llm_usage` bersifat
  organisasi — dipakai bersama semua anggota.
- Maksimal **2 produksi paralel** (`PRODUCE_CONCURRENCY`); permintaan ke-3
  menerima pesan "coba lagi beberapa saat".
- **Jangan pernah** menaikkan `--workers` di atas 1 — SqliteSaver tidak
  multi-process safe (checkpoint korup, modul dobel).
- File sesi & checkpoint hilang hanya jika volume `./runtime/` dihapus.

## 8. Pemecahan masalah cepat

| Gejala | Sebab umum | Solusi |
|---|---|---|
| RAG balasan tidak relevan tapi tak ada error | `EMBEDDING_MODEL` terisi remote di VPS | kosongkan `EMBEDDING_MODEL=`, `docker compose up -d` |
| `/healthz` gagal di healthcheck | ingest RAG pertama masih berjalan | tunggu (start-period 90s); cek `docker compose logs` |
| Login kembali setiap saat | `SESSION_SECRET` berubah tiap restart | pastikan SESSION_SECRET tetap di `.env` |
| SSE terasa macet di depan proxy | buffering | Caddy: `flush_interval -1` |
| Key ditolak (401 dari ollama.com) | key lama dirotasi/mati | perbarui `LLM_API_KEY` di `.env`, `docker compose up -d` |