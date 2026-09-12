"""
LLM FACTORY - ChatOllama terhubung ke cloud API
================================================
Semua agent mengambil LLM dari sini. Endpoint & model dikendalikan .env:
- LLM_BASE_URL, LLM_MODEL, LLM_API_KEY

Bisa menunjuk ke Ollama lokal (proxy model :cloud) atau langsung ke
https://ollama.com / provider Ollama-compatible lain — cukup ubah .env,
tanpa menyentuh kode agent.
"""

import os
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


# Tag thinking glm: ditulis lewat konkatenasi agar aman dari pemrosesan markup.
_THINK_OPEN = "<" + "think" + ">"
_THINK_CLOSE = "</" + "think" + ">"


def _strip_think(text: str) -> str:
    """Buang blok thinking dari content glm-5.3-flash:cloud.

    Model ini mem-embed fase thinking langsung di `content` (walau think
    dimatikan di request - perilaku proxy cloud). Blok tertutup memakai
    tag penutup; bila terpotong, tag buka tampil tanpa penutup.
    Cek tag penutup dulu - blok tertutup tetap mengandung tag buka.
    """
    if _THINK_CLOSE in text:
        return text.rsplit(_THINK_CLOSE, 1)[-1].lstrip()
    if _THINK_OPEN in text:  # thinking terpotong tanpa penutup
        return text.rsplit(_THINK_OPEN, 1)[-1].lstrip()
    return text


# ----------------------------------------------------------------------
# Hook streaming (ronde 4): server SSE memasang sink + callback status pada
# thread worker-nya SEBELUM graph.invoke - panggilan LLM percakapan pertama
# di thread itu langsung dialirkan token-per-token.
#
# Deployment: ContextVar, BUKAN threading.local - fan-out LangGraph (Send
# Agent2/3) berjalan di thread pool yang TIDAK mewarisi threading.local,
# tapi mewarisi contextvars (langchain copy_context). Ini memperbaiki bug:
# narasi produksi ("Menulis bagian: ...") tidak pernah sampai UI karena
# sink tidak terbaca di thread worker fan-out.
# ----------------------------------------------------------------------
_CV_SINK: ContextVar = ContextVar("llm_stream_sink", default=None)
_CV_STATUS: ContextVar = ContextVar("llm_status_hook", default=None)


class GenerationCancelled(BaseException):
    """Dibangkitkan saat user menekan tombol Stop di UI (ronde 14).

    Sengaja turunan BaseException, BUKAN Exception: agent punya banyak
    "except Exception" fallback (retry invoke_with_retry, fallback
    deterministik _dig_turn, dsb.) - cancel TIDAK boleh tertelan oleh
    semuanya (bisa berubah jadi retry atau balasan palsu). Dengan
    BaseException, cancel menembus semuanya sampai ke worker server yang
    memang menanganinya secara khusus."""


_CV_CANCEL: ContextVar = ContextVar("llm_cancel_event", default=None)


def set_cancel_event(ev) -> None:
    """Pasang threading.Event pembatalan pada context worker ini (server)."""
    _CV_CANCEL.set(ev)


def get_cancel_event():
    return _CV_CANCEL.get()


def _cancelled() -> bool:
    ev = _CV_CANCEL.get()
    return ev is not None and ev.is_set()


def set_stream_sink(sink) -> None:
    """Pasang sink streaming (objek dengan .feed(delta)) pada context ini."""
    _CV_SINK.set(sink)


def get_stream_sink():
    return _CV_SINK.get()


def set_status_hook(hook: Optional[Callable[[str], None]]) -> None:
    """Pasang callback status (dipanggil dgn label tahap LLM saat mulai)."""
    _CV_STATUS.set(hook)


def get_status_hook():
    return _CV_STATUS.get()


def _partial_suffix(text: str, tag: str) -> int:
    """Panjang ekor `text` yang merupakan awalan `tag` (tag terpotong antar-chunk)."""
    for k in range(min(len(text), len(tag) - 1), 0, -1):
        if text.endswith(tag[:k]):
            return k
    return 0


class _ThinkStream:
    """Pengupas tag think secara streaming (state machine lintas chunk).

    Bukan cukup _strip_think() di akhir: UI butuh delta yang sudah bersih
    agar balasan tampak "mengetik" tanpa membocorkan fase thinking.
    State: init (mencari tag buka) -> think (buang isi) -> out (lewatkan).
    """

    def __init__(self):
        self.state = "init"
        self.hold = ""  # ekor yang mungkin potongan tag - ditahan dulu

    def feed(self, chunk: str) -> str:
        text = self.hold + chunk
        self.hold = ""
        out = []
        while text:
            if self.state == "init":
                idx = text.find(_THINK_OPEN)
                if idx == -1:
                    keep = _partial_suffix(text, _THINK_OPEN)
                    out.append(text[: len(text) - keep])
                    self.hold = text[len(text) - keep:]
                    break
                out.append(text[:idx])
                text = text[idx + len(_THINK_OPEN):]
                self.state = "think"
            elif self.state == "think":
                idx = text.find(_THINK_CLOSE)
                if idx == -1:
                    keep = _partial_suffix(text, _THINK_CLOSE)
                    self.hold = text[len(text) - keep:]
                    break
                text = text[idx + len(_THINK_CLOSE):]
                self.state = "out"
            else:  # out
                out.append(text)
                break
        return "".join(out)

    def flush(self) -> str:
        """Delta sisa saat stream selesai."""
        text, self.hold = self.hold, ""
        if self.state == "think":
            return ""  # thinking tak pernah tertutup - buang seluruhnya
        return text


_REPLY_KEY_RE = re.compile(r'"reply"\s*:\s*"')
_JSON_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t",
                 "r": "\r", "b": "\b", "f": "\f"}


class ReplyRelay:
    """Sink streaming percakapan Agent 1 (ronde 4).

    Model menjawab dalam JSON {{"mode":.., "reply":".."}} - user TIDAK boleh
    melihat JSON mentahnya. Relay ini: (1) membuang fase thinking secara
    streaming via _ThinkStream, (2) menemukan field "reply" pada JSON,
    (3) men-decode string JSON-nya bertahap (escape \\n, \\", \\uXXXX) dan
    meneruskan potongan teks balasan ke on_text -> UI tampak mengetik jawaban
    manusia. Model yang melanggar format tidak me-relay apa pun; UI tetap
    menampilkan jawaban final dari event `final`.
    """

    def __init__(self, on_text: Callable[[str], None]):
        self.on_text = on_text
        self._strip = _ThinkStream()
        self._buf = ""
        self._scanned = 0      # offset terakhir yang sudah dicari key-nya
        self._state = "scan"   # scan -> string -> done
        self._esc = False      # menunggu karakter setelah backslash
        self._uni = None       # buffer escape \\uXXXX parsial

    def feed(self, raw_delta: str) -> None:
        vis = self._strip.feed(raw_delta)
        if vis:
            self._json_feed(vis)

    def _emit(self, text: str) -> None:
        if not text:
            return
        try:
            self.on_text(text)
        except Exception:  # noqa: BLE001 - UI mati tak boleh menggagalkan LLM
            pass

    def _json_feed(self, chunk: str) -> None:
        if self._state == "done":
            return
        self._buf += chunk
        if self._state == "scan":
            m = _REPLY_KEY_RE.search(self._buf, self._scanned)
            if not m:
                # simpan ekor kecil utk key yang terpotong antar-chunk
                self._scanned = max(0, len(self._buf) - 16)
                return
            self._buf = self._buf[m.end():]
            self._scanned = 0
            self._state = "string"
        if self._state == "string":
            self._scan_string()

    def _scan_string(self) -> None:
        out = []
        i = 0
        while i < len(self._buf):
            c = self._buf[i]
            if self._esc:
                self._esc = False
                if c == "u":
                    self._uni = "\\u"  # mulai escape unicode 4 hex
                else:
                    out.append(_JSON_ESCAPES.get(c, c))
                i += 1
                continue
            if self._uni is not None:
                self._uni += c
                i += 1
                if len(self._uni) == 6:
                    try:
                        out.append(chr(int(self._uni[2:], 16)))
                    except ValueError:
                        pass
                    self._uni = None
                continue
            if c == '"':
                self._state = "done"
                i += 1
                break
            if c == "\\":
                self._esc = True
                i += 1
                continue
            out.append(c)
            i += 1
        self._buf = self._buf[i:]
        self._emit("".join(out))


# Preset effort LLM (ronde 3): satu tabel = policy biaya seluruh sistem.
# Angka num_predict sesuai kondisi kerja terukur glm-5.3-flash:cloud.
# Ronde 4 - routing cepat/berat: effort "low" (chat ringan) pakai model
# flash cepat LLM_MODEL_FAST (terukur: dig 3.4-4.1s vs 6.3-6.5s glm-5.3);
# kerja berat (silabus, konten modul) tetap model utama glm-5.3-flash.
EFFORT_PRESETS = {
    "low":    {"temperature": 0.4, "num_predict": 4000,   # percakapan dig (reply pendek)
               "model": os.getenv("LLM_MODEL_FAST", "deepseek-v4-flash:cloud")},
    "medium": {"temperature": 0.1, "num_predict": 8000, "model": None},   # evaluator (kaku)
    "high":   {"temperature": 0.3, "num_predict": 16000, "model": None},  # agent1 build (JSON silabus)
    # Ronde 11b: draf produksi nyata terukur >60k char - 24000 token
    # TERPOTONG di tengah JSON ("Expecting ',' delimiter"). Mulai langsung
    # 48000; bila tetap menembus, invoke_with_retry menaikkan sampai 96000.
    "extra":  {"temperature": 0.4, "num_predict": 48000, "model": None},  # agent2 writer (konten panjang)
}


# ----------------------------------------------------------------------
# Narasi progres produksi (ronde 6): user harus bisa melihat apa yang
# sedang dikerjakan di background - bukan mengarang, tapi kejadian NYATA:
# key draft_json yang baru saja muncul di output streaming LLM = bagian
# yang baru saja ditulis.
_SECTION_LABELS = {
    "kata_pengantar": "Kata Pengantar",
    "pendahuluan": "bagian Pendahuluan",
    "deskripsi_unit": "Deskripsi Unit Kompetensi",
    "unit_kompetensi_detail": "rincian Unit Kompetensi",
    "batasan_variabel": "Batasan Variabel",
    "evaluasi_pengetahuan": "evaluasi Pengetahuan",
    "evaluasi_praktik": "evaluasi Praktik",
    "kamus_rows": "Kamus Istilah",
    "referensi_rows": "daftar Referensi",
    "panduan_penilaian": "panduan Penilaian",
    "lik_skenario": "skenario Lembar Informasi & Kerja",
    "lik_langkah_kerja": "langkah kerja LIK",
    "lik_peralatan": "daftar peralatan LIK",
    "bahan_rows": "tabel bahan LIK",
    "cek_hasil_rows": "checklist hasil kerja",
    "cek_observasi_rows": "checklist observasi",
    "pengetahuan_content": "materi pengetahuan",
    "panduan_penilaian_x": "panduan penilaian",
}
_KEY_RE = re.compile(r'"([a-z0-9_]+)"\s*:')


class ProductionRelay:
    """Sink streaming untuk fase PRODUKSI (approve): membaca JSON draft yang
    ditulis LLM secara bertahap dan mengumumkan bagian yang baru saja
    selesai diketik ("Menulis bagian: Pendahuluan...") - progres nyata,
    bukan drama. Setiap key baru yang muncul di stream diumumkan sekali.
    """

    def __init__(self, on_status: Callable[[str], None]):
        self.on_status = on_status
        self.persistent = True   # invoke_with_retry: JANGAN di-clear setelah pakai
        self._strip = _ThinkStream()
        self._buf = ""
        self._seen: set = set()
        self._elemen_rows = False

    def feed(self, raw_delta: str) -> None:
        vis = self._strip.feed(raw_delta)
        if vis:
            self._scan(vis)

    def _announce(self, text: str) -> None:
        try:
            self.on_status(text)
        except Exception:  # noqa: BLE001 - UI mati tak boleh menggagalkan LLM
            pass

    def _scan(self, chunk: str) -> None:
        self._buf += chunk
        if len(self._buf) > 200_000:  # jaga memori; sisa tak perlu dipindai lagi
            self._buf = self._buf[-4000:]
        if '"elemen_rows"' in self._buf and not self._elemen_rows:
            self._elemen_rows = True
            self._announce("Menyusun tabel elemen kompetensi (per baris KUK)…")
        for m in _KEY_RE.finditer(self._buf):
            key = m.group(1)
            if key in self._seen or key == "elemen_rows":
                continue
            self._seen.add(key)
            label = _SECTION_LABELS.get(key)
            if label is None:
                continue  # key identitas (judul_modul, kode_unit, ...) & verdict
                          # evaluator tidak diumumkan - bukan bagian konten
            self._announce(f"Menulis bagian: {label}…")


def get_llm_for_effort(effort: str):
    """Mode AUTO (ronde 3): LLM dibuat dari preset effort, bukan angka manual."""
    try:
        preset = EFFORT_PRESETS[effort]
    except KeyError:
        raise ValueError(f"Effort tidak dikenal: {effort!r} (pilih dari {sorted(EFFORT_PRESETS)})")
    return get_llm(**preset)


def auto_effort(task: str, *, draft: bool = False) -> str:
    """Policy AUTO: tahap mana pakai effort apa. draft=True = program tanpa
    daftar unit (Agent 1 harus mengusulkan sendiri) -> butuh effort lebih."""
    if task == "agent1.dig":
        return "low"
    if task == "agent1.penyusun":
        return "low"
    if task == "agent1.build":
        return "extra" if draft else "high"
    if task == "agent2":
        return "extra"
    if task == "agent3":
        return "medium"
    raise ValueError(f"Task tidak dikenal: {task!r}")


# Akuntansi biaya per label tahap (transparansi biaya di log & /api/info).
_USAGE: dict = {}


def get_llm_usage() -> dict:
    return {k: dict(v) for k, v in _USAGE.items()}


def get_llm(temperature: float = 0.3, num_predict: Optional[int] = None, model: Optional[str] = None):
    """Buat instance ChatOllama sesuai konfigurasi .env.

    Args:
        temperature: 0.1-0.2 untuk evaluator (kaku), 0.3-0.5 untuk writer.
        num_predict: batas token output (None = biarkan default server).
        model: override nama model (None = LLM_MODEL dari .env). Dipakai
            routing cepat/berat ronde 4: chat ringan pakai model flash cepat,
            kerja berat pakai model utama.
    """
    from langchain_ollama import ChatOllama

    api_key = os.getenv("LLM_API_KEY", "")
    params = {
        "model": model or os.getenv("LLM_MODEL", "glm-5.3-flash:cloud"),
        "base_url": os.getenv("LLM_BASE_URL", "http://localhost:11434"),
        "temperature": temperature,
    }
    if num_predict:
        params["num_predict"] = num_predict
    if api_key:
        # Provider cloud (mis. ollama.com) butuh Bearer token.
        # Ollama lokal mengabaikan header ini — aman selalu dikirim.
        params["client_kwargs"] = {"headers": {"Authorization": f"Bearer {api_key}"}}

    llm = ChatOllama(**params)

    # glm-5.2:cloud adalah reasoning model: fase "thinking" memakan budget
    # num_predict secara DIAM - budget kecil menghasilkan konten kosong.
    # PENTING utk glm-5.3-flash:cloud: JANGAN set reasoning=False. Terukur di
    # mesin ini: dengan think:false, langchain-ollama menghapus TAG think tapi
    # MENINGGALKAN teks thinking bercampur di content; dengan default (tanpa
    # think:false), content datang bersih. Jadi reasoning=False hanya utk 5.2.
    model_name = params["model"]
    if model_name.startswith("glm-5.2") and hasattr(llm, "reasoning"):
        try:
            llm.reasoning = False
        except Exception:  # noqa: BLE001 - provider lama tanpa dukungan think
            pass

    return llm


def _ollama_target(llm):
    """Header + options + endpoint /api/chat yang dipakai _raw_chat & _raw_chat_stream."""
    import os

    headers = dict((getattr(llm, "client_kwargs", None) or {}).get("headers") or {})
    if not headers and os.getenv("LLM_API_KEY", ""):
        headers["Authorization"] = f"Bearer {os.getenv('LLM_API_KEY')}"
    # num_ctx: default proxy 8192 memotong output di tengah (terukur: prompt
    # 15k chars + 4.4k token output sudah mentok). Model sendiri dukung 1M.
    # Ronde 4: budget kecil (chat ringan dig, 4k) tak butuh ctx lebar - ctx
    # 8192 terukur ~2s lebih cepat daripada 20k di proxy cloud (5.0-5.2s vs
    # 7.0-7.4s). Kerja berat tetap num_predict + 16384 agar tak terpotong.
    if llm.num_predict is not None and llm.num_predict <= 4000:
        ctx = 8192
    else:
        ctx = (llm.num_predict or 8192) + 16384
    options = {"temperature": llm.temperature, "num_ctx": ctx}
    if llm.num_predict is not None:
        options["num_predict"] = llm.num_predict
    url = str(llm.base_url).rstrip("/") + "/api/chat"
    return url, headers, options


def _ollama_prompt(llm, prompt: str) -> str:
    """/no_think: soft-switch khusus GLM - menekan fase thinking tanpa merusak
    konten (terukur: esei ringkas 2428 -> 815 token). Model non-GLM
    (mis. deepseek-v4-flash:cloud, ronde 4) TIDAK paham switch ini - malah
    merusak output JSON (terukur: clean_json False dgn suffix) - jangan
    dikirim ke selain glm."""
    return prompt + ("\n/no_think" if str(llm.model).startswith("glm") else "")


def _raw_chat(llm, prompt: str):
    """Panggil /api/chat proxy ollama secara LANGSUNG via httpx.

    Kenapa dua lapis masalahnya:
    1. langchain-ollama (think:false): menghapus TAG think tapi meninggalkan
       teks thinking bercampur jawaban - tak bisa dipisah.
    2. ollama python Client: kwarg `think` TIDAK dikirim ke proxy (versi lama)
       -> model berpikir bebas berbelitan belasan ribu token tanpa tag.
    Dengan POST httpx + "think": false, proxy menyetel thinking PENDEK dan
    menyertakan tag penutup di content -> _strip_think() membuangnya bersih.
    """
    from types import SimpleNamespace

    import httpx

    url, headers, options = _ollama_target(llm)
    body = {
        "model": llm.model,
        "messages": [{"role": "user", "content": _ollama_prompt(llm, prompt)}],
        "stream": False,
        "options": options,
    }
    r = httpx.post(url, json=body, headers=headers, timeout=600)
    r.raise_for_status()
    d = r.json()
    meta = {"eval_count": d.get("eval_count"), "done_reason": d.get("done_reason")}
    return SimpleNamespace(content=(d.get("message") or {}).get("content") or "", response_metadata=meta)


def _raw_chat_stream(llm, prompt: str, on_text):
    """Sama dengan _raw_chat tapi stream=True (ronde 4): setiap delta content
    yang sudah bebas thinking diteruskan ke on_text. Dipakai utk panggilan
    PERCAKAPAN pertama (sink ReplyRelay terpasang) supaya UI tampak mengetik -
    kerja berat (silabus/konten) tetap non-stream, hasilnya dipakai parser.

    Return objek berinterface sama dengan _raw_chat (content mentah + metadata)
    sehingga seluruh jalur retry/auto-bump/usage di invoke_with_retry tetap jalan.
    """
    import json as _json
    from types import SimpleNamespace

    import httpx

    url, headers, options = _ollama_target(llm)
    body = {
        "model": llm.model,
        "messages": [{"role": "user", "content": _ollama_prompt(llm, prompt)}],
        "stream": True,
        "options": options,
    }
    parts = []
    stripper = _ThinkStream()
    meta = {}
    # httpx.post() membaca body penuh dulu - streaming WAJIB lewat Client.stream.
    with httpx.Client(timeout=600) as client:
        with client.stream("POST", url, json=body, headers=headers) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                # Ronde 14: cek cancel per baris - Stop dari UI memutus
                # stream KE PROXY di sini (koneksi ditutup oleh context
                # manager saat exception keluar), bukan hanya di UI.
                if _cancelled():
                    raise GenerationCancelled()
                if not line or not line.strip():
                    continue
                try:
                    d = _json.loads(line)
                except ValueError:
                    continue
                if d.get("done"):
                    meta = {"eval_count": d.get("eval_count"),
                            "done_reason": d.get("done_reason")}
                    break
                delta = (d.get("message") or {}).get("content") or ""
                if delta:
                    parts.append(delta)
                    vis = stripper.feed(delta)
                    if vis:
                        on_text(vis)
    vis = stripper.flush()
    if vis:
        on_text(vis)
    return SimpleNamespace(content="".join(parts), response_metadata=meta)


def _is_chatollama(llm) -> bool:
    return bool(getattr(llm, "base_url", None) and getattr(llm, "model", None))


def invoke_with_retry(llm, prompt: str, attempts: int = 5, base_delay: float = 2.0, label: str = ""):
    """Panggil llm.invoke dengan retry + backoff untuk error jaringan transient.

    Cloud endpoint (ollama.com / proxy lokal) bisa time out sesaat — satu error
    jaringan tidak boleh membatalkan seluruh batch modul. Error terakhir tetap
    dilempar supaya node/graph bisa melaporkannya.
    Ronde 6: default 5 attempt (backoff 2/4/8/16s ~ 30s total) - 3 attempt
    dulu tak cukup utk 502 Bad Gateway proxy cloud yang berlangsung >10 detik
    (terukur 2026-09-07 menggagalkan smoke --full di tahap agent3).

    Khusus glm-5.3-flash:cloud: fase thinking menghabiskan budget num_predict
    sebelum jawaban tercetak (terukur: prompt 15k chars butuh >12k token thinking
    saja) -> konten kosong/"..." dengan done_reason="length". Bila itu terjadi,
    num_predict dilipatgandakan sekali dan dipanggil ulang.
    """
    import time

    last_exc = None
    budget = getattr(llm, "num_predict", None)
    hook = get_status_hook()
    for attempt in range(attempts):
        try:
            # Ronde 14: cancel dicek di TEPI tiap attempt - Stop saat LLM
            # non-stream sedang berjalan baru berlaku setelah panggilan itu
            # selesai (httpx blocking tidak bisa diinterupsi), tapi sisa
            # retry/attempt berikutnya langsung batal.
            if _cancelled():
                raise GenerationCancelled()
            if hook and label:
                try:
                    hook(label)
                except Exception:  # noqa: BLE001 - hook UI tak boleh menggagalkan LLM
                    pass
            # Baca sink TIAP attempt (bukan cached): sink sekali-pakai sudah
            # di-clear dari thread-local setelah pemakaian pertama.
            sink = get_stream_sink()
            if sink is not None and _is_chatollama(llm):
                # Sink biasa sekali pakai: hanya panggilan LLM pertama di thread
                # ini yang dialirkan (percakapan dig / reply build). Retry &
                # panggilan berikutnya non-stream - hasil final selalu dari
                # parser. Sink PERSISTEN (narasi progres produksi) tidak
                # di-clear agar setiap panggilan agent teralirkan.
                if not getattr(sink, "persistent", False):
                    set_stream_sink(None)
                # Tahan-banting: terima callable polos ATAU objek .feed.
                on_text = sink.feed if hasattr(sink, "feed") else sink
                response = _raw_chat_stream(llm, prompt, on_text)
            else:
                response = _raw_chat(llm, prompt) if _is_chatollama(llm) else llm.invoke(prompt)
            content = response.content if isinstance(response.content, str) else ""
            meta = response.response_metadata or {}
            # Terpotong di tengah thinking (belum ada tag tutup) atau konten
            # kosong/"..." -> budget thinking tidak cukup, lipatgandakan.
            think_unclosed = meta.get("done_reason") == "length" and _THINK_CLOSE not in content
            # Ronde 11b: done_reason="length" = output MENEMBUS batas budget -
            # termasuk JSON draf yang terpotong di tengah (laporan produksi
            # "Expecting ',' delimiter ... char 59805": draf agent2 >60k char
            # terpangkas pada 48000 token). Bump berlapis: 24k->48k->96k.
            if meta.get("done_reason") == "length" and budget and int(budget) < 96000:
                llm.num_predict = min(96000, int(budget) * 2)
                budget = llm.num_predict
                print(f"[llm_retry] output terpotong (done=length) - num_predict -> {budget}")
                continue
            empty_from_think = (
                budget
                and (not content.strip().strip("."))
            )
            if empty_from_think and budget < 48000:
                llm.num_predict = min(48000, int(budget) * 2)
                budget = llm.num_predict
                print(f"[llm_retry] konten kosong (budget habis utk thinking) - num_predict -> {budget}")
                continue
            # Sisa blok thinking yang bocor ke content dibuang di SATU titik
            # sentral agar semua parser agent (JSON dsb.) tidak rusak.
            if isinstance(response.content, str):
                response.content = _strip_think(response.content)
            if label:
                n = int(meta.get("eval_count") or 0)
                rec = _USAGE.setdefault(label, {"calls": 0, "tokens": 0})
                rec["calls"] += 1
                rec["tokens"] += n
                print(f"[effort] {label} | num_predict={getattr(llm, 'num_predict', '?')} | eval={n} | total={rec['tokens']}")
            return response
        except Exception as exc:  # noqa: BLE001 - retry semua jenis error jaringan
            last_exc = exc
            if attempt < attempts - 1:
                delay = base_delay * (2**attempt)
                print(f"[llm_retry] attempt {attempt + 1}/{attempts} gagal ({exc}); retry {delay:.0f}s")
                time.sleep(delay)
    raise last_exc