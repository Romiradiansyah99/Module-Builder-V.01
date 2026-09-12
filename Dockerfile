# ============================================================
# Kemnaker Module Builder — image produksi VPS
# ============================================================
# Build context membawa database/ (chroma_db 44,7MB + skkni_docs +
# template + program + contoh_modul) sebagai data referensi imutabel.
# Data yang BERUBAH saat runtime (uploads, output, checkpoint, sesi,
# ai_memory, cache ONNX) hidup di volume ./runtime:/app/runtime.
#
# WAJIB: single worker (SqliteSaver tidak multi-process safe).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Cache ONNX chroma (all-MiniLM) jatuh ke $HOME -> volume runtime
    HOME=/app/runtime/home

# libgomp1: dibutuhkan onnxruntime/chromadb; curl untuk HEALTHCHECK
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements dulu -> layer cache tidak invalid saat kode berubah
COPY requirements.txt .
RUN pip install -r requirements.txt

# Kode + data referensi
COPY . .

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/runtime \
    && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]