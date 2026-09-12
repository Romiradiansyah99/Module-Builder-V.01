"""
APP - UI Streamlit: Chat + Approval System (HITL)
==================================================
Menjalankan graph LangGraph (main_graph.build_graph) dengan:
1. Chat dengan Agent 1 -> draft silabus (JSON) ditampilkan sebagai tabel.
2. GATE HITL: tombol Approve -> set approved_by_human=True -> graph resume
   -> fan-out paralel Agent 2 per modul -> evaluasi -> inject .docx.
   Tombol Revisi -> feedback ditambahkan ke percakapan -> silabus baru.
3. Download file .docx hasil inject.

Jalankan: streamlit run app.py  ->  http://localhost:8501
"""

import uuid
from pathlib import Path

import streamlit as st

from agents.state import STATUS_NEED_HUMAN_REVIEW
from tools.doc_utils import OUTPUT_DIR, template_info

st.set_page_config(page_title="Kemnaker Module Builder", page_icon="📚", layout="wide")
st.title("📚 Kemnaker AI Module Builder")
st.caption("Generator Modul Pelatihan Kemnaker — LangGraph Map-Reduce + RAG SKKNI")


# ----------------------------------------------------------------------
# Resource yang di-cache: graph + ingest RAG (sekali per server)
# ----------------------------------------------------------------------
@st.cache_resource(show_spinner="Menyiapkan graph & indeks SKKNI (ingest chunk pertama kali bisa lama)...")
def load_graph():
    from main_graph import build_graph
    from tools.vector_store import ingest_skkni_docs

    ingest_skkni_docs()  # no-op jika sudah ada index di database/chroma_db
    return build_graph()


graph = load_graph()


# ----------------------------------------------------------------------
# Session state
# ----------------------------------------------------------------------
def reset_conversation():
    st.session_state.messages = []
    st.session_state.thread_id = str(uuid.uuid4())
    st.session_state.phase = "chat"  # chat | approval | running | done | error
    st.session_state.run_events = []
    st.session_state.error_msg = ""


def show_error_panel():
    """Panel error dengan jalan pulur: coba lagi / mulai baru.
    Tanpa ini, exception node membuat phase 'running' menggantung dan
    panel approval + hasil menghilang."""
    st.error(f"❌ Terjadi kesalahan: {st.session_state.error_msg}")
    left, right = st.columns(2)
    with left:
        if st.button("🔄 Mulai percakapan baru"):
            reset_conversation()
            st.rerun()
    with right:
        if st.button("⬅️ Kembali ke percakapan"):
            st.session_state.phase = "approval" if st.session_state.messages else "chat"
            st.rerun()


if "thread_id" not in st.session_state:
    reset_conversation()


def get_config():
    return {"configurable": {"thread_id": st.session_state.thread_id}}


# ----------------------------------------------------------------------
# Sidebar: info sistem
# ----------------------------------------------------------------------
with st.sidebar:
    st.subheader("Sistem")
    info = template_info()
    st.write(f"**Template:** {Path(info['path']).name} — {len(info['tags'])} tag")
    with st.expander("Daftar tag template"):
        st.write(", ".join(info["tags"]) or "(tidak ada tag)")
    st.write(f"**Output dir:** `{OUTPUT_DIR}`")
    if st.button("🔄 Mulai percakapan baru"):
        reset_conversation()
        st.rerun()


# ----------------------------------------------------------------------
# Panel hasil (approval / progress / download)
# ----------------------------------------------------------------------
def render_syllabus_panel():
    """Tabel silabus + tombol Approve / Revisi (gate HITL)."""
    snapshot = graph.get_state(get_config())
    modules = snapshot.values.get("modules") or []
    if not modules:
        return

    st.subheader("📋 Draft Silabus — Menunggu Approval")
    st.dataframe(
        {
            "ID": [m["module_id"] for m in modules],
            "Judul Modul": [m["module_title"] for m in modules],
        },
        use_container_width=True,
        hide_index=True,
    )
    for m in modules:
        with st.expander(f"{m['module_id']} — {m['module_title']}"):
            if m.get("syllabus_rows"):
                # Tabel silabus terstruktur (elemen/kuk/indikator/pengetahuan/keterampilan/durasi)
                st.table([
                    {
                        "Elemen": f"{r.get('elemen_no', '')} {r.get('elemen', '')}".strip(),
                        "KUK": f"{r.get('kuk_no', '')} {r.get('kuk', '')}".strip(),
                        "Indikator": r.get("indikator", "") or "-",
                        "Pengetahuan": r.get("pengetahuan", "") or "-",
                        "Keterampilan & Sikap": r.get("keterampilan", "") or "-",
                        "Durasi": r.get("durasi", "") or "-",
                    }
                    for r in m["syllabus_rows"]
                ])
            else:
                st.markdown(m["syllabus_content"])

    left, right = st.columns([1, 2])
    with left:
        if st.button("✅ APPROVE — Mulai Produksi Modul", type="primary", use_container_width=True):
            approve_and_run(modules)
    with right:
        with st.form("revisi_form"):
            feedback = st.text_area("Atau minta revisi (opsional):", placeholder="Contoh: tambahkan modul tentang K3, alokasi waktu kurang pas...")
            if st.form_submit_button("✏️ Revisi Silabus", use_container_width=True):
                if feedback.strip():
                    st.session_state.messages.append({"role": "user", "content": f"Revisi silabus: {feedback.strip()}"})
                else:
                    st.session_state.messages.append({"role": "user", "content": "Tolong perbaiki/review ulang silabusnya."})
                reset_thread_keep_chat()
                st.rerun()


def approve_and_run(modules):
    """Set approved_by_human=True lalu resume graph (fan-out paralel)."""
    graph.update_state(get_config(), {"approved_by_human": True})
    st.session_state.phase = "running"
    events = []
    progress = st.progress(0.0, text="Produksi modul berjalan (Agent 2 paralel per modul → evaluasi → rakit .docx)...")

    try:
        # stream updates: tiap event = {node_name: output}
        for event in graph.stream(None, get_config(), stream_mode="updates"):
            for node, payload in event.items():
                events.append((node, payload))
                done_mods = [m for m in (graph.get_state(get_config()).values.get("modules") or [])
                             if m.get("status_evaluasi")]
                pct = min(1.0, len(done_mods) / max(1, len(modules)))
                progress.progress(pct, text=f"Node: {node} — {len(done_mods)}/{len(modules)} modul selesai dievaluasi")
    except Exception as exc:  # noqa: BLE001 - tampilkan error, jangan biarkan phase menggantung
        progress.empty()
        st.session_state.error_msg = str(exc)
        st.session_state.phase = "error"
        st.rerun()

    progress.empty()
    st.session_state.phase = "done"
    st.session_state.run_events = events
    st.rerun()


def reset_thread_keep_chat():
    """Thread baru (riwayat chat dipertahankan) untuk revisi silabus."""
    st.session_state.thread_id = str(uuid.uuid4())
    st.session_state.phase = "chat"


def render_result_panel():
    """Status tiap modul + tombol download .docx."""
    snapshot = graph.get_state(get_config())
    values = snapshot.values
    st.subheader("✅ Produksi Selesai")
    docs = values.get("final_documents") or []

    col1, col2 = st.columns([2, 1])
    with col1:
        st.dataframe(
            {
                "ID": [m["module_id"] for m in values.get("modules") or []],
                "Judul": [m["module_title"] for m in values.get("modules") or []],
                "Status": [m.get("status_evaluasi", "-") for m in values.get("modules") or []],
                "Iterasi": [m.get("iteration_count", 0) for m in values.get("modules") or []],
            },
            use_container_width=True,
            hide_index=True,
        )
        needs_review = [m for m in values.get("modules") or [] if m.get("status_evaluasi") == STATUS_NEED_HUMAN_REVIEW]
        if needs_review:
            st.warning(f"{len(needs_review)} modul di-PASS paksa setelah 3 iterasi (Need Human Review): "
                      + ", ".join(m["module_id"] for m in needs_review))
    with col2:
        st.metric("File .docx dihasilkan", len(docs))

    if docs:
        st.subheader("⬇️ Unduh Modul")
        for path_str in docs:
            path = Path(path_str)
            if path.exists():
                with open(path, "rb") as f:
                    st.download_button(
                        f"📄 {path.name}",
                        data=f.read(),
                        file_name=path.name,
                        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        key=f"dl_{path.name}",
                        use_container_width=True,
                    )


# ----------------------------------------------------------------------
# Chat UI
# ----------------------------------------------------------------------
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

if st.session_state.phase == "approval":
    render_syllabus_panel()
elif st.session_state.phase == "done":
    render_result_panel()
    st.divider()
elif st.session_state.phase == "error":
    show_error_panel()
    st.divider()

# chat_input hanya aktif di phase "chat" — mengetik saat approval/running akan
# menggandakan chat_history (operator.add) dan me-resume graph secara diam.
chat_disabled = st.session_state.phase in ("approval", "running", "done")
user_input = st.chat_input(
    "Tulis kebutuhan pelatihan Anda... (mis. 'buat modul pelatihan PLTS sesuai program')",
    disabled=chat_disabled,
)
if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})

    # Tampilkan pesan user + jalankan Agent 1 (graph berhenti di interrupt)
    with st.chat_message("user"):
        st.markdown(user_input)
    with st.chat_message("assistant"):
        with st.spinner("Agent 1 menyusun silabus (RAG SKKNI + program pelatihan)..."):
            try:
                graph.invoke(
                    {
                        "chat_history": st.session_state.messages,
                        "program_name": "",
                        "modules": [],
                        "approved_by_human": False,
                        "final_documents": [],
                    },
                    get_config(),
                )
            except Exception as exc:  # noqa: BLE001 - error panel, bukan traceback mentah
                st.session_state.error_msg = str(exc)
                st.session_state.phase = "error"
                st.rerun()

    snapshot = graph.get_state(get_config())
    if "Map_Modules" in snapshot.next:
        # Graph berhenti sebelum Map_Modules (HITL gate) -> tampilkan silabus
        values = snapshot.values
        reply = values.get("chat_history", [])
        if reply:
            st.session_state.messages.append({"role": "assistant", "content": reply[-1]["content"]})
        st.session_state.phase = "approval"
        st.rerun()
    else:
        # Tidak ada interrupt (mis. Agent 1 error / modul kosong) -> cek error
        reply = snapshot.values.get("chat_history", [])
        if reply:
            st.session_state.messages.append({"role": "assistant", "content": reply[-1]["content"]})
        st.rerun()