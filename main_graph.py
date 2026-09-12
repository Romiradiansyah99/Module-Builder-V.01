"""
MAIN GRAPH - Routing & Workflow LangGraph (Map-Reduce / Fan-out)
=================================================================
Topologi graph:

    START -> Agent1_Syllabus -> [HITL: interrupt sebelum Map_Modules]
         -> Map_Modules --(Send API: fan-out paralel per modul)--> Agent2_Node
         -> Agent2_Node --(Send: lanjut modul yang sama)---------> Agent3_Node
         -> Agent3_Node -- FAIL & iter < 3 --> Send kembali Agent2_Node (loop revisi)
                       -- PASS / iterasi habis -----------------> Inject_Word (fan-in)
         -> Inject_Word -> END

Catatan desain:
- Setiap worker (Agent2/Agent3) menerima SATU ModuleState via Send, dan
  mengembalikan {"modules": [modul terbaru]} -> di-merge ke GlobalState
  oleh reducer upsert merge_modules (lihat agents/state.py).
- Inject_Word baru dieksekusi SETELAH semua worker paralel selesai
  (fan-in barrier alami LangGraph), lalu merakit semua .docx sekaligus.
"""

from typing import Optional

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from agents.agent1_syllabus import agent1_node
from agents.agent2_content import agent2_node
from agents.agent3_evaluator import agent3_node
from agents.state import STATUS_FAIL, GlobalState, ModuleState
from tools.word_injector import inject_all_modules


# ----------------------------------------------------------------------
# Node passthrough: anchor untuk interrupt_before=["Map_Modules"].
# Setelah manusia approve (UI set approved_by_human=True lalu resume),
# node ini berjalan dan conditional edge-nya membelah (fan-out) modul.
# ----------------------------------------------------------------------
def map_modules_node(state: GlobalState) -> dict:
    """Titik penggabungan interrupt HITL + pemicu fan-out."""
    return {}


def inject_word_node(state: GlobalState, config: RunnableConfig) -> dict:
    """Node akhir (fan-in): rakit semua draf menjadi file .docx fisik.

    Deployment: output diisolasi per-thread (output/<thread_id>/) agar
    produksi paralel dua user tidak saling menimpa file. Tanpa thread_id
    (pemakaian standalone/smoke test) fallback ke OUTPUT_DIR utama.

    Ronde 10b: annotation WAJIB RunnableConfig (bukan dict). LangGraph 1.2.10
    hanya menyuntikkan config ke node jika tipe parameternya pas; annotation
    dict menyebabkan config TIDAK dipass -> TypeError "missing 1 required
    positional argument: 'config'" di ujung produksi (setelah semua PASS)."""
    from tools.doc_utils import OUTPUT_DIR

    thread_id = ((config or {}).get("configurable") or {}).get("thread_id", "")
    out_dir = OUTPUT_DIR / str(thread_id) if thread_id else OUTPUT_DIR
    paths = inject_all_modules(state, out_dir)
    return {"final_documents": [str(p) for p in paths]}


# ----------------------------------------------------------------------
# Routing functions (conditional edges)
# ----------------------------------------------------------------------
def route_from_map(state: GlobalState):
    """Fan-out: 1 Send per modul -> worker Agent 2 berjalan PARALEL.

    Guard ganda: tanpa modul atau tanpa approval manusia, tidak ada
    edge (graph berhenti dengan sopan - harusnya tak terjadi karena
    interrupt sudah mem-pause sebelum node ini).
    """
    modules = state.get("modules") or []
    if not modules or not state.get("approved_by_human"):
        return []
    return [Send("Agent2_Node", m) for m in modules]


def _latest_module(invocation_state: dict) -> ModuleState:
    """Ambil modul terbaru dari state invokasi worker.

    State invokasi = payload Send + update node. Kandidat utama ada di
    bawah key "modules" (output node); fallback ke field payload.
    """
    mods = invocation_state.get("modules") or []
    if mods and isinstance(mods[-1], dict) and "module_id" in mods[-1]:
        return mods[-1]
    return {k: invocation_state[k] for k in ModuleState.__annotations__ if k in invocation_state}


def route_after_agent2(invocation_state: dict):
    """Agent 2 selesai -> kirim modul + drafnya ke evaluator (Send)."""
    return Send("Agent3_Node", _latest_module(invocation_state))


def route_after_agent3(invocation_state: dict):
    """Loop kondisional: FAIL -> kembali ke Agent 2. PASS -> fan-in Word."""
    module = _latest_module(invocation_state)
    if module.get("status_evaluasi") == STATUS_FAIL:
        return Send("Agent2_Node", module)  # revisi: kembali ke writer
    return "Inject_Word"  # PASS / PASS (Need Human Review) -> fan-in


# ----------------------------------------------------------------------
# Graph assembly
# ----------------------------------------------------------------------
def build_graph(checkpointer: Optional[object] = None):
    """Rakit dan compile graph. `checkpointer` wajib untuk HITL interrupt.

    interrupt_before=["Map_Modules"] -> graph berhenti setelah Agent 1
    selesai, menunggu Streamlit UI mengirim sinyal approved_by_human=True.
    """
    builder = StateGraph(GlobalState)

    builder.add_node("Agent1_Syllabus", agent1_node)
    builder.add_node("Map_Modules", map_modules_node)
    builder.add_node("Agent2_Node", agent2_node)
    builder.add_node("Agent3_Node", agent3_node)
    builder.add_node("Inject_Word", inject_word_node)

    builder.add_edge(START, "Agent1_Syllabus")
    builder.add_edge("Agent1_Syllabus", "Map_Modules")
    builder.add_conditional_edges(
        "Map_Modules",
        route_from_map,
        ["Agent2_Node"],  # kemungkinan tujuan (untuk rendering graph)
    )
    builder.add_conditional_edges(
        "Agent2_Node",
        route_after_agent2,
        ["Agent3_Node"],
    )
    builder.add_conditional_edges(
        "Agent3_Node",
        route_after_agent3,
        ["Agent2_Node", "Inject_Word"],
    )
    builder.add_edge("Inject_Word", END)

    if checkpointer is None:
        checkpointer = InMemorySaver()

    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=["Map_Modules"],  # HITL gate: approval silabus
    )


if __name__ == "__main__":
    # Render topologi graph ke konsol (quick sanity check tanpa menjalankan LLM)
    graph = build_graph()
    print(graph.get_graph().draw_ascii())