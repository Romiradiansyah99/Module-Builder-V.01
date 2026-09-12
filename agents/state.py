"""
STATE DEFINITIONS - Kemnaker AI Module Generator
=================================================
Definisi state untuk LangGraph (Map-Reduce / Fan-out architecture).

CATATAN DESAIN (penting dibaca sebelum lanjut ke main_graph.py):
-----------------------------------------------------------------
1. `modules` memakai reducer `merge_modules` (upsert by `module_id`).
   Alasannya: saat Fan-out via Send API, SEMUA worker Agent 2 berjalan
   paralel dan masing-masing menulis hasil revisinya ke key `modules`.
   Tanpa reducer, LangGraph akan melempar InvalidUpdateError karena
   ada update ganda ke key yang sama dari node paralel. Dengan reducer
   upsert-by-id, hasil tiap worker "menimpa" modul yang lama secara
   idempotent, dan Agent 1 cukup mengembalikan list penuh (upsert ke
   state kosong == assignment biasa).

2. `chat_history` dan `final_documents` memakai `operator.add` karena
   bersifat append-only (pesan chat & path file .docx hasil inject).
"""

from typing import Annotated, List, TypedDict
import operator

# Batas iterasi revisi Agent 2 <-> Agent 3 (anti infinite-loop).
# Jika iterasi ke-3 masih FAIL, Agent 3 memaksa PASS dengan catatan
# "Need Human Review" (aturan PRD bagian 4C).
MAX_ITERATIONS = 3

# Status evaluasi yang mungkin dihasilkan Agent 3
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_NEED_HUMAN_REVIEW = "PASS (Need Human Review)"


def merge_modules(
    existing: List["ModuleState"],
    new: List["ModuleState"],
) -> List["ModuleState"]:
    """Reducer untuk key `modules`: upsert by module_id.

    - Agent 1 (node awal) mengembalikan list silabus lengkap -> karena
      state awal kosong, hasilnya sama dengan assignment biasa.
    - Tiap worker Agent 2/3 (hasil Send API fan-out) mengembalikan SATU
      modul yang sudah diperbarui -> menimpa entri lama dengan
      module_id yang sama, tanpa menduplikasi.
    """
    by_id = {m["module_id"]: m for m in existing}
    for m in new:
        by_id[m["module_id"]] = m
    return list(by_id.values())


class ModuleState(TypedDict):
    """State untuk setiap Modul yang berjalan PARALEL (dibawa Send API)."""
    module_id: str                # identitas unik, dipakai reducer upsert
    module_title: str             # = judul unit kompetensi (sumber: daftar unit program)
    syllabus_content: str         # markdown turunan dari syllabus_rows (dipakai prompt Agent 2/3)
    syllabus_rows: List[dict]     # silabus TERSTRUKTUR per baris: elemen/kuk/indikator/pengetahuan/keterampilan/durasi
    kode_unit: str                # kode unit kompetensi (mis. E.38SPH02.014.01)
    alokasi_waktu: str            # perkiraan waktu unit (mis. "16 JP @ 45 menit")
    penyusun: dict                # ronde 12: {"nama","profesi","nama2","profesi2"} - ditanya Agent 1 di awal percakapan
    draft_json: dict              # output Agent 2 (harus map dengan tag docxtpl)
    evaluator_feedback: str       # catatan revisi dari Agent 3
    status_evaluasi: str          # "PASS" atau "FAIL" (atau PASS w/ human review)
    iteration_count: int          # maks MAX_ITERATIONS, agar tidak infinite loop


class GlobalState(TypedDict):
    """State Utama (Orchestrator)."""
    # operator.add: tiap node menambah pesan baru tanpa menimpa yang lama
    chat_history: Annotated[list, operator.add]
    program_name: str
    # reducer upsert by module_id -- lihat catatan desain di atas
    modules: Annotated[List[ModuleState], merge_modules]
    approved_by_human: bool       # di-set True oleh Streamlit UI (HITL gate)
    # operator.add: node Word Injector menambah path file .docx yang sudah jadi
    final_documents: Annotated[List[str], operator.add]


def make_module(
    module_id: str,
    module_title: str,
    syllabus_content: str,
    syllabus_rows: List[dict] = None,
    kode_unit: str = "",
    alokasi_waktu: str = "",
    penyusun: dict = None,
) -> ModuleState:
    """Helper: buat ModuleState baru dengan nilai default yang benar."""
    return ModuleState(
        module_id=module_id,
        module_title=module_title,
        syllabus_content=syllabus_content,
        syllabus_rows=list(syllabus_rows) if syllabus_rows else [],
        kode_unit=kode_unit,
        alokasi_waktu=alokasi_waktu,
        penyusun=dict(penyusun) if penyusun else {},
        draft_json={},
        evaluator_feedback="",
        status_evaluasi="",
        iteration_count=0,
    )