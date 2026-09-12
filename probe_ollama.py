"""Probe koneksi: Ollama proxy lokal & Ollama Cloud (chat + embedding).

Jalankan: python probe_ollama.py
Baca API key dari .env (LLM_API_KEY). Tidak mengubah state apa pun.
"""
import json
from pathlib import Path

import urllib.request

ROOT = Path(__file__).resolve().parent


def load_env():
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


def post(url, payload, api_key=None, timeout=25):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")[:400]
    except Exception as exc:  # noqa: BLE001
        body = ""
        if hasattr(exc, "read"):
            try:
                body = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
        return f"GAGAL: {exc.__class__.__name__}: {exc} {body}"


env = load_env()
KEY = env.get("LLM_API_KEY", "")
print("API key:", KEY[:8] + "..." if KEY else "(kosong)")

print("\n--- 1. LLM chat via proxy lokal (glm-5.2:cloud) ---")
print(post("http://localhost:11434/api/chat",
           {"model": "glm-5.2:cloud", "messages": [{"role": "user", "content": "balas PONG saja"}], "stream": False}))

print("\n--- 2. Embedding via proxy lokal (nomic-embed-text:cloud) ---")
print(post("http://localhost:11434/api/embed", {"model": "nomic-embed-text:cloud", "input": "tes"}))

print("\n--- 3. Embedding via proxy lokal (nomic-embed-text, tanpa :cloud) ---")
print(post("http://localhost:11434/api/embed", {"model": "nomic-embed-text", "input": "tes"}))

print("\n--- 4. LLM chat langsung ke ollama.com ---")
print(post("https://ollama.com/api/chat",
           {"model": "glm-5.2:cloud", "messages": [{"role": "user", "content": "balas PONG saja"}], "stream": False},
           api_key=KEY))

print("\n--- 5. Embedding langsung ke ollama.com (nomic-embed-text:cloud) ---")
print(post("https://ollama.com/api/embed", {"model": "nomic-embed-text:cloud", "input": "tes"}, api_key=KEY))

print("\n--- 6. Embedding langsung ke ollama.com (snowflake-arctic-embed:cloud) ---")
print(post("https://ollama.com/api/embed", {"model": "snowflake-arctic-embed:cloud", "input": "tes"}, api_key=KEY))