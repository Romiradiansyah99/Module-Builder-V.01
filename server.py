"""
SERVER - FastAPI backend untuk dashboard demo Kemnaker Module Builder
=====================================================================
Membungkus graph LangGraph yang SAMA dengan app.py (Streamlit) — graph
tidak diubah sama sekali. Menyediakan:

  GET  /                    -> dashboard (static/index.html)
  GET  /login               -> halaman login kata sandi tunggal (deployment)
  GET  /api/info            -> status sistem (template tags, RAG, output)
  POST /api/login           -> login -> cookie sesi ditandatangani
  POST /api/chat            -> kirim kebutuhan pelatihan -> Agent 1 + RAG (fallback)
  POST /api/chat/stream     -> SSE: balasan Agent 1 mengalir token-per-token
  POST /api/approve         -> SSE stream: approve HITL -> fan-out paralel
  GET  /api/state/{tid}     -> snapshot state thread (untuk restore UI)
  GET  /api/download/{fn}   -> unduh file .docx dari output/
  GET  /healthz             -> healthcheck (tanpa auth, dipakai Docker)

Jalankan:
    uvicorn server:app --reload --port 8000
    (atau) python server.py

Deployment (tahap deployment): auth kata sandi tunggal, checkpoint SqliteSaver
+ sesi persisten (bertahan restart), program aktif per-sesi (ContextVar),
output per-thread, guard approve ganda + batas konkurensi produksi.
Single process WAJIB (SqliteSaver tidak multi-process safe).
"""

import json
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional, Tuple

# Konsol Windows default cp1252: emoji dari balasan LLM bikin print meledak.
# Paksa UTF-8 + replace agar log server tidak pernah crash.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - stream non-reconfigurable diabaikan
        pass

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agents.state import ModuleState
from tools.ai_memory import start_background_update
from tools.auth import (
    auth_configured,
    check_password,
    cookie_kwargs,
    make_session_token,
    verify_session_token,
)
from tools.doc_utils import OUTPUT_DIR, get_active_program, set_context_program, template_info
from tools.llm_config import (
    GenerationCancelled,
    set_cancel_event,
    set_status_hook,
    set_stream_sink,
)
from tools.session_store import load as load_sessions
from tools.session_store import save as save_sessions
from rag.threads import THREADS as RAG_THREADS


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Ingest RAG SKKNI sekali di thread belakang saat server menyala."""
    threading.Thread(target=_ingest_background, daemon=True).start()
    # RAG chat: auto-index program docx aktif (best-effort, tidak memblokir).
    threading.Thread(target=_ingest_rag_background, daemon=True).start()
    yield


# ----------------------------------------------------------------------
# Auth kata sandi tunggal (deployment): satu dependency menjaga SEMUA
# rute - SSE dan /api/download ikut terlindungi (cookie same-origin
# dikirim otomatis oleh fetch). Tanpa APP_PASSWORD_SHA256/SESSION_SECRET
# (dev lokal) server jalan tanpa auth.
# ----------------------------------------------------------------------
_PUBLIC_PATHS = {"/login", "/api/login", "/healthz"}


def require_auth(request: Request):
    if not auth_configured():
        return  # mode dev lokal tanpa konfigurasi auth
    path = request.url.path
    if path in _PUBLIC_PATHS:
        return
    if verify_session_token(request.cookies.get("kb_session")):
        return
    if path.startswith("/api/"):
        raise HTTPException(401, "Belum login. Muat ulang halaman untuk login.")
    # Dependency app-level membuang nilai balikan, tapi EXCEPTION selalu
    # diteruskan: 303 + Location = browser mengikuti ke /login.
    raise HTTPException(status_code=303, headers={"Location": "/login"}, detail="Login diperlukan.")


app = FastAPI(
    title="Kemnaker Module Builder API",
    lifespan=lifespan,
    dependencies=[Depends(require_auth)],
)

_PROJECT_ROOT = Path(__file__).resolve().parent


def _resolve_env_path(name: str, default: Path) -> Path:
    """Path dari env; relatif di-resolve terhadap root proyek (bukan CWD)."""
    val = (os.getenv(name) or "").strip()
    if not val:
        return default
    p = Path(val)
    return p if p.is_absolute() else _PROJECT_ROOT / p


_UPLOADS_DIR = _resolve_env_path("UPLOADS_DIR", _PROJECT_ROOT / "database" / "uploads")

# Batas produksi paralel (proteksi belanja LLM cloud saat dipakai tim).
_PRODUCE_SEMAPHORE = threading.Semaphore(max(1, int(os.getenv("PRODUCE_CONCURRENCY", "2"))))

# ----------------------------------------------------------------------
# Resource global (satu graph + satu ingest RAG untuk semua session demo)
# ----------------------------------------------------------------------
_graph = None
_graph_lock = threading.Lock()
_rag_ready = False
_rag_error: Optional[str] = None

# Session demo: thread_id -> {"chat_messages": [...], "phase": str, "program": str}
# Persisten (runtime/sessions.json via tools/session_store.py) - restart
# server tidak membuang sesi tim lagi. Checkpoint graph juga persisten
# (SqliteSaver, runtime/checkpoints.sqlite).
SESSIONS: Dict[str, dict] = load_sessions()


def get_graph():
    global _graph
    if _graph is None:
        with _graph_lock:
            if _graph is None:
                from main_graph import build_graph

                # Checkpoint persisten: silabus/produksi selamat restart.
                # SATU proses saja (SqliteSaver tidak multi-process safe).
                from langgraph.checkpoint.sqlite import SqliteSaver

                db_path = _resolve_env_path(
                    "CHECKPOINT_DB", _PROJECT_ROOT / "runtime" / "checkpoints.sqlite"
                )
                db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(str(db_path), check_same_thread=False)
                _graph = build_graph(checkpointer=SqliteSaver(conn))
    return _graph


def _ingest_background():
    """Ingest RAG SKKNI di thread belakang (no-op jika index sudah ada)."""
    global _rag_ready, _rag_error
    try:
        from tools.vector_store import ingest_skkni_docs

        ingest_skkni_docs()
        _rag_ready = True
        print("[server] RAG SKKNI siap.")
    except Exception as exc:  # noqa: BLE001 - jangan biarkan server mati
        _rag_error = str(exc)
        print(f"[server] RAG ingest GAGAL: {exc}")


def _ingest_rag_background():
    """Auto-index program docx aktif utk RAG chat (best-effort)."""
    try:
        from rag.document_manager import ingest_program

        rec = ingest_program()
        if rec:
            print(f"[server] RAG chat siap (program: {rec.get('chunk_count', 0)} chunk).")
    except Exception as exc:  # noqa: BLE001 - jangan biarkan server mati
        print(f"[server] RAG chat ingest program GAGAL (diabaikan): {exc}")


def _wait_rag() -> None:
    """Tunggu ingest selesai. Timeout dari RAG_WAIT_TIMEOUT (default 60s);
    gagal/tidak siap = 503 dengan pesan jelas (bukan 500 generik)."""
    timeout = float(os.getenv("RAG_WAIT_TIMEOUT", "60"))
    deadline = time.time() + timeout
    while not _rag_ready and _rag_error is None and time.time() < deadline:
        time.sleep(0.5)
    if _rag_error:
        raise HTTPException(503, f"RAG ingest gagal: {_rag_error}")
    if not _rag_ready:
        raise HTTPException(503, "RAG belum siap (ingest masih berjalan) - coba lagi sebentar.")


def _config(thread_id: str):
    return {"configurable": {"thread_id": thread_id}}


# Ronde 14 (tombol Stop): event pembatalan per thread - di-set oleh
# /api/stop/{thread_id}, dicek di dalam loop streaming LLM (llm_config).
_CANCELLED: Dict[str, threading.Event] = {}

# RAG chat: event per thread (analog _CANCELLED, tapi rute /api/rag/stop).
_RAG_CANCELLED: Dict[str, threading.Event] = {}


# ----------------------------------------------------------------------
# Schemas
# ----------------------------------------------------------------------
class ChatRequest(BaseModel):
    thread_id: Optional[str] = None
    message: str


class RagChatRequest(BaseModel):
    thread_id: Optional[str] = None
    message: str
    context_strategy: Optional[str] = None


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _invoke_agent1(thread_id: str, chat_messages: list, continuing: bool = False) -> dict:
    """Jalankan graph sampai interrupt HITL. Return snapshot values.

    continuing=True: thread sudah punya state (program_name/modules) - kirim
    HANYA chat_history baru. Full-init dict akan MENIMPA program_name/modules
    lama dengan kosong (LangGraph men-apply input apa adanya).
    """
    graph = get_graph()
    if continuing:
        payload = {"chat_history": chat_messages}
    else:
        payload = {
            "chat_history": chat_messages,
            "program_name": "",
            "modules": [],
            "approved_by_human": False,
            "final_documents": [],
        }
    # Program per-sesi: cap ke ContextVar (bukan global) agar percakapan
    # user lain tidak terkontaminasi upload satu user. ContextVar mewarisi
    # ke thread fan-out Agent2/3; threading.local tidak. Reset di finally
    # karena thread pool AnyIO dipakai ulang antar request.
    token = set_context_program((SESSIONS.get(thread_id) or {}).get("program"))
    try:
        graph.invoke(payload, _config(thread_id))
        snapshot = graph.get_state(_config(thread_id))
        values = snapshot.values
        if "Map_Modules" not in (snapshot.next or ()):
            raise RuntimeError(
                "Graph tidak berhenti di gate approval (Agent 1 mungkin gagal) - cek log server."
            )
        return values
    finally:
        set_context_program(None)


class ChatResponse(BaseModel):
    thread_id: str
    phase: str
    program_name: str
    modules: list
    reply: str


# Status manusiawi per tahap LLM (ronde 4) - dikirim ke UI selama menunggu.
STATUS_LABELS = {
    "agent1.dig": "Membaca program & menggali kebutuhan…",
    "agent1.build": "Menyusun draft silabus…",
    "agent2": "Menulis materi modul…",
    "agent3": "Memeriksa kualitas modul…",
}


def _prepare_chat(req: ChatRequest) -> Tuple[str, list, bool, dict]:
    """Logika sesi/thread bersama utk /api/chat dan /api/chat/stream.

    Return (thread_id, seed, continuing, session)."""
    message = req.message.strip()
    if not message:
        raise HTTPException(400, "Pesan kosong.")

    old_thread = req.thread_id if req.thread_id in SESSIONS else None
    session = SESSIONS.get(old_thread) if old_thread else None

    # Ronde 9: timestamp utk daftar riwayat chat di sidebar (/api/threads).
    def _touch(s: dict, new: bool = False) -> None:
        now = time.time()
        if new:
            s.setdefault("created", now)
        s["updated"] = now

    if session and session["phase"] == "approval":
        # Revisi silabus: thread BARU, riwayat chat dipertahankan + pesan revisi
        session["chat_messages"].append({"role": "user", "content": message})
        thread_id = str(uuid.uuid4())
        SESSIONS[thread_id] = {
            "chat_messages": list(session["chat_messages"]),
            "phase": "chat",
            # Program mengikuti sesi lama (bukan global terakhir yang berubah)
            "program": session.get("program") or get_active_program(),
        }
        _touch(SESSIONS[thread_id], new=True)
        seed = list(SESSIONS[thread_id]["chat_messages"])
        continuing = False  # thread baru -> full init
    elif old_thread:
        thread_id = old_thread
        session["chat_messages"].append({"role": "user", "content": message})
        seed = [{"role": "user", "content": message}]  # graph state sudah memuat sisanya
        continuing = True
        _touch(session)
        if "program" not in session:  # sesi lama pra-upgrade: cap sekarang
            session["program"] = get_active_program()
    else:
        thread_id = str(uuid.uuid4())
        SESSIONS[thread_id] = {
            # Ronde 14: pesan user ikut tersimpan sejak awal - dulu baru
            # muncul setelah finalize, sehingga bila dibatalkan/diputus
            # pesan user hilang dari riwayat sesi.
            "chat_messages": [{"role": "user", "content": message}],
            "phase": "chat",
            # Dicap saat thread dibuat: upload diikuti thread baru (frontend
            # me-reset thread_id saat upload), jadi program sesi = aktif kini.
            "program": get_active_program(),
        }
        _touch(SESSIONS[thread_id], new=True)
        seed = [{"role": "user", "content": message}]
        continuing = False
    save_sessions(SESSIONS)
    return thread_id, seed, continuing, SESSIONS[thread_id]


def _finalize_chat(thread_id: str, values: dict) -> dict:
    """Simpan hasil invoke ke sesi + susun payload respons (dipakai kedua
    endpoint chat). Phase ditentukan hasil Agent 1: modul ada = silabus siap
    di-approve; modul kosong = Agent 1 masih tanya cakupan."""
    modules = values.get("modules") or []
    phase = "approval" if modules else "chat"
    SESSIONS[thread_id]["phase"] = phase
    SESSIONS[thread_id]["updated"] = time.time()
    chat_history = values.get("chat_history") or []
    reply = chat_history[-1]["content"] if chat_history else "Silabus siap."
    SESSIONS[thread_id]["chat_messages"].append({"role": "assistant", "content": reply})
    save_sessions(SESSIONS)
    return {
        "thread_id": thread_id,
        "phase": phase,
        "program_name": values.get("program_name", ""),
        "modules": modules,
        "reply": reply,
    }


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/login")
def login_page():
    """Halaman login kata sandi tunggal (deployment tim)."""
    return FileResponse(Path(__file__).parent / "static" / "login.html")


class LoginRequest(BaseModel):
    password: str


@app.post("/api/login")
def api_login(req: LoginRequest):
    """Cek sandi -> cookie sesi ditandatangani (HttpOnly, 30 hari)."""
    if not auth_configured():
        raise HTTPException(
            400, "Auth belum dikonfigurasi (isi APP_PASSWORD_SHA256 & SESSION_SECRET di .env)."
        )
    if not check_password(req.password):
        raise HTTPException(401, "Kata sandi salah.")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(value=make_session_token(), **cookie_kwargs())
    return resp


@app.get("/healthz")
def healthz():
    """Healthcheck tanpa auth (dipakai Docker HEALTHCHECK / uptime monitor)."""
    return {"ok": True, "rag_ready": _rag_ready, "rag_error": _rag_error}


@app.get("/api/info")
def info():
    t = template_info()
    from tools.ai_memory import load as load_ai_memory
    from tools.doc_utils import get_active_program
    from tools.llm_config import get_llm_usage

    return {
        "template_tags": t["tags"],
        "template_exists": t["exists"],
        "rag_ready": _rag_ready,
        "rag_error": _rag_error,
        "output_dir": str(OUTPUT_DIR),
        "active_program": get_active_program(),
        # Transparansi biaya mode AUTO (ronde 3): token per tahap LLM.
        "llm_usage": get_llm_usage(),
        # Ronde 5: aturan sikap yang dipelajari dari sesi-sesi sebelumnya.
        "ai_memory": load_ai_memory(),
    }


@app.get("/api/units")
def units():
    """Daftar unit kompetensi program aktif (ekstraksi deterministik docx) -
    untuk chip saran di sambutan UI."""
    from tools.unit_extractor import extract_program_meta, extract_program_units

    try:
        units = list(extract_program_units())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Ekstraksi unit gagal: {exc}")
    return {"program_title": extract_program_meta().get("judul", ""), "units": units}


@app.post("/api/program/upload")
async def program_upload(file: UploadFile = File(...)):
    """Upload program pelatihan milik user (feedback ronde 2 item 4).

    Program final maupun DRAFT diterima: draft tanpa tabel daftar unit tetap
    sah (units_available=false) - Agent 1 akan mengusulkan unit dari SKKNI.
    """
    from tools.doc_utils import set_active_program
    from tools.unit_extractor import extract_program_meta, extract_program_units

    name = file.filename or ""
    if not name.lower().endswith(".docx"):
        raise HTTPException(400, "Hanya file .docx yang didukung.")
    content = await file.read()
    if not content:
        raise HTTPException(400, "File kosong.")

    _UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\-. ]+", "_", name).strip() or "program.docx"
    # Prefix UUID: dua user mengunggah file bernama sama di detik yang sama
    # tidak saling menimpa (timestamp lama tabrakan pada upload paralel).
    dest = _UPLOADS_DIR / f"{uuid.uuid4().hex[:8]}_{safe}"
    dest.write_bytes(content)

    try:
        from docx import Document

        Document(str(dest))
    except Exception as exc:  # noqa: BLE001 - file rusak: buang & tolak
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"File .docx tidak valid: {exc}")

    set_active_program(dest)
    try:
        meta = extract_program_meta(str(dest))
        units = list(extract_program_units(str(dest)))
    except Exception as exc:  # noqa: BLE001 - docx valid tapi ekstraksi kacau
        raise HTTPException(400, f"Ekstraksi program gagal: {exc}")

    units_data = [
        {k: u.get(k, "") for k in ("no", "kelompok", "judul", "kode", "jumlah", "alokasi")}
        for u in units
    ]
    print(f"[server] Program aktif diganti: {dest.name} ({len(units)} unit)")
    return {
        "program_title": meta.get("judul", ""),
        "units": units_data,
        "units_available": bool(units),
        "file": dest.name,
        "active_program": str(dest),
    }


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    """Versi non-stream (fallback bila SSE gagal di jaringan/proxy)."""
    _wait_rag()
    thread_id, seed, continuing, _session = _prepare_chat(req)
    try:
        values = _invoke_agent1(thread_id, seed, continuing=continuing)
    except Exception as exc:  # noqa: BLE001 - laporkan sebagai 500 bermakna
        raise HTTPException(500, f"Agent 1 gagal: {exc}")
    response = _finalize_chat(thread_id, values)
    # Ronde 5: rangkum percakapan -> ilmu sikap (background, tak memblokir).
    start_background_update(SESSIONS[thread_id]["chat_messages"])
    return ChatResponse(**response)


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest):
    """Chat streaming SSE (ronde 4) - interaksi terasa langsung, bukan
    menggantung sampai LLM selesai penuh.

    Event (satu per baris, prefixed "data: "):
      {"type":"thread","thread_id":"…"}                    # id thread (awal)
      {"type":"status","text":"Menyusun draft silabus…"}   # progress per tahap
      {"type":"token","text":"…"}                          # delta balasan
      {"type":"final","response":{...ChatResponse...}}
      {"type":"cancelled"}                                 # dihentikan user
      {"type":"error","message":"…"}

    Sink + hook dipasang di thread worker (thread-local di llm_config) -
    panggilan LLM PERCAKAPAN pertama (dig, atau reply build) mengalir ke UI;
    delta JSON/thinking difilter ReplyRelay sehingga user hanya melihat teks
    balasan. Graph tetap tidak diubah sama sekali."""
    _wait_rag()
    thread_id, seed, continuing, _session = _prepare_chat(req)

    # Ronde 14: event Stop per thread - di-clear di awal agar stream ulang
    # pada thread yang sama tidak mewarisi cancel sebelumnya.
    cancel_ev = _CANCELLED.setdefault(thread_id, threading.Event())
    cancel_ev.clear()

    q: queue.Queue = queue.Queue()

    def sink(text: str):
        q.put(("token", text))

    def status_hook(label: str):
        text = STATUS_LABELS.get(label)
        if text:
            # stage ikut dikirim - UI memakainya utk rotasi teks variatif ala
            # Claude Code (poin 1 ronde 6) tanpa mengarang aktivitas.
            q.put(("status", {"text": text, "stage": label}))

    def worker():
        # Sink WAJIB objek ReplyRelay (invoke_with_retry memanggil .feed):
        # fungsi polos menyebabkan AttributeError -> retry 3x gagal -> Agent 1
        # jatuh ke balasan deterministik (bug terukur 2026-09-07).
        from tools.llm_config import ReplyRelay

        set_cancel_event(cancel_ev)   # ronde 14: Stop dicek di dalam loop LLM
        set_stream_sink(ReplyRelay(sink))
        set_status_hook(status_hook)
        try:
            values = _invoke_agent1(thread_id, seed, continuing=continuing)
            # Finalisasi sesi di worker: konsisten walau client menutup koneksi.
            response = _finalize_chat(thread_id, values)
            q.put(("final", response))
            # Ronde 5: rangkum percakapan -> ilmu sikap (background).
            start_background_update(SESSIONS[thread_id]["chat_messages"])
        except GenerationCancelled:
            # Ronde 14: pesan user yang dibatalkan tetap diserahkan ke state
            # graph (reducer chat_history) - tanpa ini pesan hilang dari
            # konteks Agent 1 walau ada di sesi, dan kirim berikutnya tampak
            # "lupa" terhadap pesan itu. Checkpoint tidak berubah karena
            # invoke batal di tengah node.
            try:
                get_graph().update_state(
                    _config(thread_id), {"chat_history": seed}
                )
            except Exception:  # noqa: BLE001 - sinkronisasi state best-effort
                pass
            print(f"[server] Streaming dibatalkan user: {thread_id[:8]}")
            q.put(("cancelled", None))
        except Exception as exc:  # noqa: BLE001 - laporkan lewat SSE
            q.put(("error", f"Agent 1 gagal: {exc}"))
        finally:
            set_cancel_event(None)
            set_stream_sink(None)
            set_status_hook(None)
            _CANCELLED.pop(thread_id, None)

    threading.Thread(target=worker, daemon=True).start()

    def event_stream():
        # Ronde 14: id thread dikirim SEKETIKA (sebelum worker selesai) -
        # tombol Stop UI membutuhkannya utk memanggil /api/stop/{thread_id}
        # (thread baru dari pesan pertama belum diketahui client sebelumnya).
        yield f"data: {json.dumps({'type': 'thread', 'thread_id': thread_id})}\n\n"
        while True:
            try:
                kind, payload = q.get(timeout=0.25)
            except queue.Empty:
                yield ": keepalive\n\n"  # jaga koneksi tetap hidup
                continue
            if kind == "token":
                yield f"data: {json.dumps({'type': 'token', 'text': payload}, ensure_ascii=False)}\n\n"
            elif kind == "status":
                yield f"data: {json.dumps({'type': 'status', **payload}, ensure_ascii=False)}\n\n"
            elif kind == "final":
                yield f"data: {json.dumps({'type': 'final', 'response': payload}, ensure_ascii=False)}\n\n"
                break
            elif kind == "cancelled":
                yield f"data: {json.dumps({'type': 'cancelled'})}\n\n"
                break
            else:  # error
                yield f"data: {json.dumps({'type': 'error', 'message': payload}, ensure_ascii=False)}\n\n"
                break

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/stop/{thread_id}")
def stop_stream(thread_id: str):
    """Ronde 14: hentikan streaming percakapan yang sedang berjalan.

    Meng-set event cancel yang dicek per baris oleh loop streaming LLM
    (llm_config._raw_chat_stream) -> panggilan naik sebagai
    GenerationCancelled (menembus fallback/retry agent). Idempoten: thread
    tanpa stream berjalan tetap OK (worker sudah selesai / belum ada)."""
    ev = _CANCELLED.get(thread_id)
    if ev is not None:
        ev.set()
    return {"ok": True, "stopped": ev is not None}


@app.post("/api/approve/{thread_id}")
def approve(thread_id: str):
    """HITL approve -> SSE stream event produksi paralel.

    Event (satu per baris, prefixed "data: "):
      {"type":"phase","phase":"running"}
      {"type":"progress","text":"Menulis materi modul M01 · ..."}  # narasi nyata
      {"type":"status","text":"Menulis bagian: Pendahuluan…","stage":"agent2.section"}
      {"type":"module","module":{...}}     # update terbaru per modul
      {"type":"documents","paths":[...]}   # file .docx jadi
      {"type":"done"} / {"type":"error","message":...}

    Ronde 6: worker + queue (pola chat_stream) - graph berjalan di thread
    worker sehingga sink/status-hook thread-local dipasang di thread yang
    SAMA dengan panggilan LLM. Narasi progres berbasis kejadian nyata:
    node yang selesai, modul yang sedang ditulis, bagian draft yang baru
    muncul di output streaming (ProductionRelay), dan dokumen yang tersimpan.
    """
    session = SESSIONS.get(thread_id)
    if not session:
        raise HTTPException(404, "Thread tidak dikenal. Mulai percakapan baru.")
    if session["phase"] not in ("approval", "done"):
        raise HTTPException(409, f"Thread sedang phase '{session['phase']}'.")
    if not _PRODUCE_SEMAPHORE.acquire(blocking=False):
        limit = os.getenv("PRODUCE_CONCURRENCY", "2")
        raise HTTPException(
            429, f"Ada {limit} produksi lain sedang berjalan. Coba lagi beberapa saat."
        )

    # Fase "producing" di-set SEKETIKA (sebelum worker menyala): dua klik
    # approve hampir bersamaan tidak lagi memicu dua produksi paralel.
    session["phase"] = "producing"
    session["updated"] = time.time()
    save_sessions(SESSIONS)

    q: queue.Queue = queue.Queue()

    def status_hook(label: str):
        text = STATUS_LABELS.get(label)
        if text:
            q.put(("status", {"text": text, "stage": label}))

    def worker():
        from tools.llm_config import ProductionRelay

        set_stream_sink(ProductionRelay(
            lambda s: q.put(("status", {"text": s, "stage": "agent2.section"}))
        ))
        set_status_hook(status_hook)
        # Program per-sesi juga untuk fase produksi (Agent1 revisi tak dijalankan
        # di sini, tapi konsistensi context tetap dijaga lintas thread).
        ctx_token = set_context_program(session.get("program"))
        try:
            graph = get_graph()
            graph.update_state(_config(thread_id), {"approved_by_human": True})
            for event in graph.stream(None, _config(thread_id), stream_mode="updates"):
                for node, payload in event.items():
                    if node == "Map_Modules":
                        continue
                    modules = (payload or {}).get("modules")
                    if modules:
                        for m in modules:
                            q.put(("module", {"node": node, "module": _slim(m)}))
                            # Narasi nyata per tahap node (ronde 6 poin 5).
                            if node == "Agent2_Node":
                                q.put(("progress", {
                                    "text": f"Menulis materi modul {m.get('module_id')} · "
                                            f"{m.get('module_title')}…",
                                    "stage": "agent2",
                                }))
                            elif node == "Agent3_Node":
                                q.put(("progress", {
                                    "text": f"Memeriksa kualitas {m.get('module_id')} · "
                                            f"{m.get('module_title')} "
                                            f"(putaran {m.get('iteration_count', 1)})…",
                                    "stage": "agent3",
                                }))
                    docs = (payload or {}).get("final_documents")
                    if docs:
                        names = ", ".join(Path(p).name for p in docs)
                        q.put(("progress", {"text": f"Menyusun dokumen Word: {names}…",
                                            "stage": "word"}))
                        q.put(("documents", docs))
                    q.put(("node", node))
            session["phase"] = "done"
            session["updated"] = time.time()
            save_sessions(SESSIONS)
            q.put(("done", None))
        except Exception as exc:  # noqa: BLE001 - kirim error ke UI
            # Produksi gagal: silabus tetap sah, kembalikan ke approval agar
            # user bisa menekan approve lagi tanpa chat ulang.
            session["phase"] = "approval"
            save_sessions(SESSIONS)
            q.put(("error", str(exc)))
        finally:
            set_context_program(None)
            _PRODUCE_SEMAPHORE.release()
            set_stream_sink(None)
            set_status_hook(None)

    threading.Thread(target=worker, daemon=True).start()

    def event_stream():
        yield f"data: {json.dumps({'type': 'phase', 'phase': 'running'})}\n\n"
        while True:
            try:
                kind, payload = q.get(timeout=0.25)
            except queue.Empty:
                yield ": keepalive\n\n"  # jaga koneksi tetap hidup
                continue
            if kind == "status":
                yield f"data: {json.dumps({'type': 'status', **payload}, ensure_ascii=False)}\n\n"
            elif kind == "progress":
                yield f"data: {json.dumps({'type': 'progress', **payload}, ensure_ascii=False)}\n\n"
            elif kind == "module":
                yield f"data: {json.dumps({'type': 'module', **payload}, ensure_ascii=False)}\n\n"
            elif kind == "documents":
                yield f"data: {json.dumps({'type': 'documents', 'paths': payload}, ensure_ascii=False)}\n\n"
            elif kind == "node":
                yield f"data: {json.dumps({'type': 'node', 'node': payload})}\n\n"
            elif kind == "done":
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                break
            else:  # error
                yield f"data: {json.dumps({'type': 'error', 'message': payload}, ensure_ascii=False)}\n\n"
                break

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _slim(module) -> dict:
    """ModuleState -> versi ringan untuk UI (draft_json diringkas).

    syllabus_rows ikut dibawa (bukan syllabus_content) - bentuk paling ringkas
    untuk render tabel silabus Word-like di UI."""
    m: ModuleState = module
    draft = m.get("draft_json") or {}
    filled = {k: (v if isinstance(v, (list, int, float)) else str(v)[:120]) for k, v in draft.items()}
    return {
        "module_id": m.get("module_id"),
        "module_title": m.get("module_title"),
        "kode_unit": m.get("kode_unit", ""),
        "alokasi_waktu": m.get("alokasi_waktu", ""),
        "syllabus_rows": m.get("syllabus_rows") or [],
        "status_evaluasi": m.get("status_evaluasi", ""),
        "iteration_count": m.get("iteration_count", 0),
        "evaluator_feedback": (m.get("evaluator_feedback") or "")[:500],
        "draft_filled": len(draft),
        "draft_preview": filled,
    }


@app.get("/api/threads")
def threads():
    """Ronde 9: daftar riwayat percakapan utk sidebar.

    Judul = pesan user pertama (dipotong 64 char). Semua sesi muncul -
    chat baru TIDAK menghapus yang lama (sesi server persisten)."""
    items = []
    for tid, sess in SESSIONS.items():
        msgs = sess.get("chat_messages") or []
        first_user = next((m["content"] for m in msgs if m.get("role") == "user"), "")
        title = " ".join(first_user.split())
        if len(title) > 64:
            title = title[:63].rstrip() + "…"
        items.append({
            "thread_id": tid,
            "title": title or "Percakapan",
            "phase": sess.get("phase", "chat"),
            "n_messages": len(msgs),
            "updated": sess.get("updated") or 0,
        })
    items.sort(key=lambda x: (x["updated"], x["thread_id"]), reverse=True)
    return {"threads": items}


@app.get("/api/state/{thread_id}")
def state(thread_id: str):
    session = SESSIONS.get(thread_id)
    if not session:
        raise HTTPException(404, "Thread tidak dikenal.")
    snapshot = get_graph().get_state(_config(thread_id))
    values = snapshot.values or {}
    return {
        "thread_id": thread_id,
        "phase": session["phase"],
        "program_name": values.get("program_name", ""),
        "modules": [_slim(m) for m in values.get("modules") or []],
        "documents": values.get("final_documents") or [],
        "chat_messages": session["chat_messages"],
    }


@app.get("/api/download/{filename}")
def download(filename: str):
    # Dokumen kini di output/<thread_id>/ (isolasi per-thread) - cari rekursif.
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(404, "File tidak ditemukan.")
    matches = [p for p in OUTPUT_DIR.rglob(filename) if p.is_file()]
    if not matches:
        raise HTTPException(404, "File tidak ditemukan.")
    path = matches[0].resolve()
    if OUTPUT_DIR.resolve() not in path.parents:
        raise HTTPException(404, "File tidak ditemukan.")
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


# ======================================================================
# RAG CHAT (tanya dokumen) - pola SSE chat_stream, endpoint terpisah agar
# pipeline modul /api/chat* tidak tersentuh.
# ======================================================================

# Status manusiawi per tahap RAG chat (dikirim apa adanya; UI merotasi
# varian sendiri per stage lewat STAGE_VARIANTS di index.html).
RAG_STATUS_LABELS = {
    "rag.rewrite": "Merapikan pertanyaan agar pencarian akurat…",
    "rag.retrieve": "Mencari referensi dokumen paling relevan…",
}


@app.post("/api/rag/docs/upload")
async def rag_upload(file: UploadFile = File(...)):
    """Upload dokumen (PDF/DOCX) -> chunk -> embed ke rag_chat collection."""
    name = file.filename or ""
    lower = name.lower()
    ext = ".pdf" if lower.endswith(".pdf") else (".docx" if lower.endswith(".docx") else "")
    if not ext:
        raise HTTPException(400, "Hanya file .pdf dan .docx yang didukung.")
    content = await file.read()
    if not content:
        raise HTTPException(400, "File kosong.")

    rag_dir = _UPLOADS_DIR / "rag_chat"
    rag_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\-. ]+", "_", name).strip() or f"dokumen{ext}"
    dest = rag_dir / f"{uuid.uuid4().hex[:8]}_{safe}"
    dest.write_bytes(content)

    try:
        from rag.chunker import load_document

        load_document(dest)  # validasi bisa diurai (PDF/DOCX)
    except Exception as exc:  # noqa: BLE001 - file rusak: buang & tolak
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"File tidak valid: {exc}")

    from rag.chunker import doc_id_for_bytes
    from rag.document_manager import ingest

    doc_id = doc_id_for_bytes(content)
    rec = ingest(doc_id, "upload", dest, name)
    if rec.get("error"):
        raise HTTPException(422, rec["error"])
    print(f"[server] RAG dokumen di-index: {name} ({rec.get('chunk_count', 0)} chunk)")
    return {"ok": True, **rec}


@app.get("/api/rag/docs")
def rag_docs():
    """Daftar dokumen di rag_chat collection (grouped per doc_id)."""
    from rag.document_manager import list_documents

    return {"docs": list_documents()}


@app.delete("/api/rag/docs/{doc_id}")
def rag_delete(doc_id: str):
    """Hapus satu dokumen (semua chunk-nya) + file di disk."""
    from rag.document_manager import delete_document

    removed = delete_document(doc_id)
    return {"ok": True, "deleted_chunks": removed}


@app.post("/api/rag/stop/{thread_id}")
def rag_stop_stream(thread_id: str):
    """Hentikan streaming jawaban RAG chat yang berjalan (idempoten)."""
    ev = _RAG_CANCELLED.get(thread_id)
    if ev is not None:
        ev.set()
    return {"ok": True, "stopped": ev is not None}


@app.post("/api/rag/chat/stream")
def rag_chat_stream(req: RagChatRequest):
    """SSE tanya-jawab atas dokumen (Rewrite-Retrieve-Read + streaming).

    Event (satu per baris, prefixed "data: "):
      {"type":"thread","thread_id":"…"}
      {"type":"status","text":"…","stage":"rag.rewrite"|"rag.retrieve"}
      {"type":"sources","docs":[{source,page,score,source_type},…]}
      {"type":"token","text":"…"}                 # delta jawaban
      {"type":"final","response":{thread_id,answer,sources}}
      {"type":"cancelled"} / {"type":"error","message":"…"}

    Worker + queue (pola chat_stream): graph/LLM berjalan di thread worker
    sehingga cancel-event terbaca; sink streaming dipasang di chat.py.
    """
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(400, "Pesan kosong.")
    thread_id = req.thread_id or str(uuid.uuid4())

    cancel_ev = _RAG_CANCELLED.setdefault(thread_id, threading.Event())
    cancel_ev.clear()

    q: queue.Queue = queue.Queue()

    def status_hook(text: str, stage: str):
        q.put(("status", {"text": text, "stage": stage}))

    def worker():
        from tools.llm_config import GenerationCancelled
        from rag.chat import run_rag_chat

        set_cancel_event(cancel_ev)
        try:
            result = run_rag_chat(
                thread_id,
                message,
                status_cb=status_hook,
                token_cb=lambda delta: q.put(("token", delta)),
                sources_cb=lambda docs: q.put(("sources", docs)),
                context_strategy=req.context_strategy,
            )
            q.put(("final", result))
        except GenerationCancelled:
            q.put(("cancelled", None))
        except Exception as exc:  # noqa: BLE001 - laporkan lewat SSE
            q.put(("error", f"RAG chat gagal: {exc}"))
        finally:
            set_cancel_event(None)
            _RAG_CANCELLED.pop(thread_id, None)

    threading.Thread(target=worker, daemon=True).start()

    def event_stream():
        yield f"data: {json.dumps({'type': 'thread', 'thread_id': thread_id})}\n\n"
        while True:
            try:
                kind, payload = q.get(timeout=0.25)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if kind == "token":
                yield f"data: {json.dumps({'type': 'token', 'text': payload}, ensure_ascii=False)}\n\n"
            elif kind == "status":
                yield f"data: {json.dumps({'type': 'status', **payload}, ensure_ascii=False)}\n\n"
            elif kind == "sources":
                yield f"data: {json.dumps({'type': 'sources', 'docs': payload}, ensure_ascii=False)}\n\n"
            elif kind == "final":
                yield f"data: {json.dumps({'type': 'final', 'response': payload}, ensure_ascii=False)}\n\n"
                break
            elif kind == "cancelled":
                yield f"data: {json.dumps({'type': 'cancelled'})}\n\n"
                break
            else:  # error
                yield f"data: {json.dumps({'type': 'error', 'message': payload}, ensure_ascii=False)}\n\n"
                break

    return StreamingResponse(event_stream(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    # Tahap 0 deployment: bind env-driven agar bisa jalan di VPS/Docker
    # (HOST=0.0.0.0). Default tetap 127.0.0.1 untuk pemakaian lokal.
    uvicorn.run(
        app,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
    )