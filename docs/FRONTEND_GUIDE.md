# Frontend Guide — API & SSE Contract

This document is the **complete contract between the backend (`server.py`, FastAPI) and the frontend (`static/index.html`)**. It exists so a frontend can be redesigned or replaced (e.g. by an AI frontend generator) without breaking the pipeline.

The current frontend is a single-file, no-build-step SPA (`static/index.html`). A replacement may use any stack, but it **must speak the protocol below**.

---

## 1. Auth

- Every request (except `/login`, `/api/login`, `/healthz`) requires the signed session cookie `kb_session` (HttpOnly).
- Login: `POST /api/login` with `{"password": "…"}` → `200` + cookie, `401` on wrong password.
- Unauthorized API calls return `401` → frontend must redirect to `/login`.
- Dev note: if `APP_PASSWORD_SHA256` / `SESSION_SECRET` are unset in `.env`, auth is disabled (local dev).

## 2. Core object model

- **Thread** = one conversation. `thread_id` (UUID) is created by the server on the first message of a conversation.
- **Phase lifecycle** (returned in every relevant response):

  | Phase | Meaning | UI should show |
  |---|---|---|
  | `chat` | Agent 1 (curriculum consultant) is dialoguing — asking about scope, units, target participants | chat bubbles |
  | `approval` | A syllabus (one or more modules) is ready → waiting for human approval | chat + syllabus table + Approve/Revise buttons |
  | `producing` | Production pipeline running (Agent 2 writer → Agent 3 QA loop → Word injection) | live production panel; chat stays usable |
  | `done` | `.docx` documents ready for download | download cards |

  Important: **chat remains available while production runs.** The user can switch threads or continue chatting; production events must be rendered into a per-thread panel, never into the chat of the wrong thread.

## 3. REST endpoints

| Method & path | Body / params | Returns |
|---|---|---|
| `GET /` | — | serves `static/index.html` |
| `GET /login` | — | serves `static/login.html` |
| `POST /api/login` | `{"password"}` | `{"ok": true}` + session cookie; `401` if wrong |
| `GET /healthz` | — | `{"ok", "rag_ready", "rag_error"}` (no auth) |
| `GET /api/info` | — | template tags, RAG readiness, active program path, `llm_usage`, `ai_memory` (learned style rules) |
| `GET /api/units` | — | `{"program_title", "units": [{no, kelompok, judul, kode, jumlah, alokasi}]}` — for suggestion chips |
| `POST /api/program/upload` | multipart `file` (.docx) | `{"program_title", "units", "units_available", "file", "active_program"}` — replaces active program; accepts drafts (no unit table) too |
| `POST /api/chat` | `{"thread_id"?, "message"}` | **non-stream fallback**: `ChatResponse {thread_id, phase, program_name, modules, reply}` |
| `POST /api/chat/stream` | `{"thread_id"?, "message"}` | **SSE** (see §4) — primary chat endpoint |
| `POST /api/stop/{thread_id}` | — | `{"ok", "stopped"}` — cancels a running chat stream server-side (idempotent) |
| `POST /api/approve/{thread_id}` | — | **SSE** (see §5) — starts production |
| `GET /api/threads` | — | `{"threads": [{thread_id, title, phase, n_messages, updated}]}` — sidebar history |
| `GET /api/state/{thread_id}` | — | full snapshot: `{thread_id, phase, program_name, modules[], documents[], chat_messages[]}` — used to restore a thread |
| `GET /api/download/{filename}` | — | the generated `.docx` (Content-Disposition attachment) |

- `ChatResponse.modules` is a list of **slim modules**: `{module_id, module_title, kode_unit, alokasi_waktu, syllabus_rows[], status_evaluasi, iteration_count, evaluator_feedback, draft_filled, draft_preview}`.
- `syllabus_rows` is the Word-style syllabus table; each row: `{elemen_no?, elemen, kuk_no, kuk, indikator, pengetahuan, keterampilan, durasi}`. Render with **rowspan grouping on `elemen`** (column format mirrors the official Kemnaker syllabus table).
- `phase == "approval"` (modules present) → show the approval card with the syllabus table(s), a **Revise** path (just send a new chat message) and an **Approve** button (`POST /api/approve/{thread_id}`).

## 4. SSE — `POST /api/chat/stream`

Body: `{"thread_id": null | "<uuid>", "message": "…"}`.
Response: `text/event-stream`; every event is `data: {json}\n\n`; a `: keepalive` comment arrives every ~0.25 s while the worker thinks.

| Event | Payload | Meaning |
|---|---|---|
| `thread` | `{thread_id}` | sent **first** — store it immediately (needed by Stop before any other event) |
| `status` | `{text, stage}` | progress label per pipeline stage (`stage`: `agent1.dig`, `agent1.build`, `agent2`, `agent3`); UI rotates variant texts per stage |
| `token` | `{text}` | delta of the assistant reply — append & render progressively (markdown; a blinking caret while streaming is nice) |
| `final` | `{response: ChatResponse}` | done — update thread id, phase; if `phase == "approval"` render the approval card |
| `cancelled` | `{}` | user pressed Stop; keep partial text and mark "— stopped" |
| `error` | `{message}` | fatal; show message |

**Stop button semantics (required):**
- While streaming, the send button becomes a **Stop** button → `POST /api/stop/{thread_id}` using the id from the `thread` event (the user may have switched threads meanwhile), then abort the fetch with `AbortController`.
- The textarea **stays editable** while the agent answers; only *sending* is locked.
- On abort/cancel: render the text received so far plus a "stopped" note. Do **not** resend the message.

## 5. SSE — `POST /api/approve/{thread_id}`

Starts the map-reduce production graph. Events (same wire format):

| Event | Payload | Meaning |
|---|---|---|
| `phase` | `{phase:"running"}` | immediately after request |
| `progress` | `{text, stage}` | human narration of **real** events: module being written (`agent2`), QA round N (`agent3`), Word doc being assembled (`word`) |
| `status` | `{text, stage:"agent2.section"}` | which draft section the writer LLM is currently streaming |
| `module` | `{node, module}` | latest slim-module update (render/update a module card) |
| `documents` | `{paths: [...]}` | finished `.docx` paths — render download buttons (`/api/download/<basename>`) |
| `node` | `{node}` | graph node completed (raw; optional to show) |
| `done` | `{}` | production finished; phase → `done` |
| `error` | `{message}` | production failed → phase rolls back to `approval` (user can approve again) |

Rules:
- Parallel production is supported (up to `PRODUCE_CONCURRENCY`, default 2; a 5th request returns `429`). Keep **one panel per thread** (`Map` of runs), ordered newest-first.
- Approve does **not** block chatting in other threads.

## 6. Features the current frontend implements (parity checklist)

- [ ] Sidebar: brand, thread history (newest first, click to restore via `/api/state/{id}`), "new chat" (never deletes old threads), program upload (resets `thread_id` to null so the new program binds to the next thread)
- [ ] ChatGPT-style chat column with markdown rendering (strips `<think>` blocks), user bubble vs assistant reply
- [ ] Suggestion chips from `/api/units` on empty state
- [ ] Streaming with status spinner + rotating stage variants + elapsed timer, then token-by-token reply with caret
- [ ] Stop button (server-side cancel) while keeping the textarea typeable
- [ ] Approval card: syllabus table with element rowspan, Approve + revision input
- [ ] Production panel: per-run module cards (status badges: running / pass / need review), live narration lines, download cards when `documents` arrive
- [ ] Multi-thread: switching threads never loses a running stream; replies land in the thread they belong to
- [ ] 401 → redirect to `/login`

## 7. Design language (current)

Apple-style bright: white surfaces on `#fbfbfd`, `#1d1d1f` text, hairline borders `rgba(0,0,0,.06–.09)`, single accent — Kemnaker navy `#15406a` with teal `#09b8a7` secondary, SF font stack, radii 10/14/16/20, soft shadows, motion 120–250 ms `cubic-bezier(.3,.15,.25,1)`, `prefers-reduced-motion` respected. Replace freely, but keep the app **in Indonesian (Bahasa Indonesia)** — the product's users are Kemnaker training staff.

## 8. Local dev

```bash
pip install -r requirements.txt
cp .env.example .env        # fill LLM_API_KEY etc. (never commit .env)
python server.py            # http://localhost:8000
```

The first boot rebuilds the RAG index from `database/skkni_docs/` (a few minutes). `python smoke_test.py` validates the whole stack without an LLM key.