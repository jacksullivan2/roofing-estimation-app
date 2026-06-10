"""Project persistence + document handling.

A 'project' is a roofing job being prepared for estimation. It carries:
  - metadata (name, client, reference, created/updated timestamps)
  - uploaded documents (file bytes on disk; metadata in JSON)
  - context answers (qid -> value) captured against the Question Map

Storage layout under the persistent data dir:
  projects.json                  index: {id: {summary fields}}
  project_<id>.json              full record (metadata + documents + answers)
  uploads/<id>/<filename>        raw uploaded files

All JSON writes go through local_store's atomic helper. The index is kept in
sync with each per-project record so the list page is one cheap read.
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

from app import question_map, settings
from app.infra import local_store

_INDEX_FILE = "projects.json"

# Document "sections" — each upload area on the project page tags its files so
# they render and export under the right heading. "project" is the general
# documents area; the others are dedicated sections.
SECTION_PROJECT = "project"
SECTION_QUALIFICATIONS = "qualifications"
SECTION_JOB_TERMS = "job_terms"
SECTION_CLIENT_TERMS = "client_terms"
DOC_SECTIONS = {
    SECTION_PROJECT, SECTION_QUALIFICATIONS,
    SECTION_JOB_TERMS, SECTION_CLIENT_TERMS,
}
# Sections that also support a free-text entry (keyed in rec["section_text"]).
TEXT_SECTIONS = {SECTION_QUALIFICATIONS}

# Filename of the compiled context document produced by "Generate the estimate".
# Re-generating replaces the previous one rather than piling up copies.
GENERATED_CONTEXT_NAME = "Project Context Summary.md"


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _now() -> float:
    return time.time()


def _safe_filename(name: str) -> str:
    """Strip path components and dangerous characters from an upload name."""
    name = Path(name or "").name
    name = re.sub(r"[^A-Za-z0-9._ \-()]+", "_", name).strip()
    return name or "file"


def _record_file(pid: str) -> str:
    return f"project_{pid}.json"


def _load_index() -> dict:
    return local_store.read_json(_INDEX_FILE, default={})


def _save_index(idx: dict) -> None:
    local_store.write_json(_INDEX_FILE, idx)


def _index_summary(rec: dict) -> dict:
    return {
        "id": rec["id"],
        "name": rec.get("name", ""),
        "client": rec.get("client", ""),
        "reference": rec.get("reference", ""),
        "created_at": rec.get("created_at"),
        "updated_at": rec.get("updated_at"),
        "n_documents": len(rec.get("documents", [])),
        "n_answers": sum(1 for v in rec.get("answers", {}).values() if _has_value(v)),
    }


def _has_value(v) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() != ""
    if isinstance(v, (list, tuple)):
        return len(v) > 0
    return True


# --------------------------------------------------------------------------- #
# Projects CRUD                                                               #
# --------------------------------------------------------------------------- #

def list_projects() -> list[dict]:
    idx = _load_index()
    items = list(idx.values())
    items.sort(key=lambda r: r.get("updated_at") or 0, reverse=True)
    return items


def get_project(pid: str) -> dict | None:
    if not pid:
        return None
    rec = local_store.read_json(_record_file(pid), default=None)
    if rec is not None:
        _normalise(rec)
    return rec


def _normalise(rec: dict) -> dict:
    """Back-fill fields added after a record was first written, so older
    projects keep working without a migration step."""
    rec.setdefault("answers", {})
    rec.setdefault("section_text", {})
    rec.setdefault("markup_pct", None)
    rec.setdefault("waste_pct", None)
    for d in rec.get("documents", []):
        d.setdefault("section", SECTION_PROJECT)
    return rec


def documents_in(rec: dict, section: str) -> list[dict]:
    return [d for d in rec.get("documents", [])
            if d.get("section", SECTION_PROJECT) == section]


def section_text(rec: dict, section: str) -> str:
    return (rec.get("section_text") or {}).get(section, "")


def create_project(name: str, client: str = "", reference: str = "") -> dict:
    pid = uuid.uuid4().hex[:12]
    rec = {
        "id": pid,
        "name": (name or "Untitled project").strip(),
        "client": (client or "").strip(),
        "reference": (reference or "").strip(),
        "created_at": _now(),
        "updated_at": _now(),
        "documents": [],
        "answers": {},
        "section_text": {},
        "markup_pct": None,
        "waste_pct": None,
    }
    local_store.write_json(_record_file(pid), rec)
    idx = _load_index()
    idx[pid] = _index_summary(rec)
    _save_index(idx)
    return rec


def _persist(rec: dict) -> dict:
    rec["updated_at"] = _now()
    local_store.write_json(_record_file(rec["id"]), rec)
    idx = _load_index()
    idx[rec["id"]] = _index_summary(rec)
    _save_index(idx)
    return rec


def delete_project(pid: str) -> bool:
    rec = get_project(pid)
    if not rec:
        return False
    # Remove uploaded files.
    pdir = local_store.uploads_dir() / pid
    if pdir.exists():
        for f in pdir.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            pdir.rmdir()
        except OSError:
            pass
    # Remove record file + index entry.
    rec_path = local_store.base_dir() / _record_file(pid)
    try:
        rec_path.unlink(missing_ok=True)
    except OSError:
        pass
    idx = _load_index()
    idx.pop(pid, None)
    _save_index(idx)
    return True


# --------------------------------------------------------------------------- #
# Documents                                                                   #
# --------------------------------------------------------------------------- #

def ext_allowed(filename: str) -> bool:
    return Path(filename).suffix.lower() in settings.ALLOWED_DOC_EXTS


def add_documents(pid: str, files: list[tuple[str, bytes]],
                  section: str = SECTION_PROJECT) -> tuple[dict, list[str]]:
    """Save uploaded (filename, bytes) pairs under `section`. Returns
    (record, skipped_reasons)."""
    if section not in DOC_SECTIONS:
        section = SECTION_PROJECT
    rec = get_project(pid)
    if not rec:
        raise KeyError(pid)
    pdir = local_store.uploads_dir() / pid
    pdir.mkdir(parents=True, exist_ok=True)

    skipped: list[str] = []
    existing = {d["filename"] for d in rec["documents"]}
    for raw_name, data in files:
        name = _safe_filename(raw_name)
        if not data:
            skipped.append(f"{name}: empty file")
            continue
        if not ext_allowed(name):
            skipped.append(f"{name}: unsupported file type")
            continue
        # De-dupe by appending a counter (filenames are unique per project dir).
        final = name
        n = 1
        while final in existing:
            stem = Path(name).stem
            suf = Path(name).suffix
            final = f"{stem} ({n}){suf}"
            n += 1
        (pdir / final).write_bytes(data)
        existing.add(final)
        rec["documents"].append({
            "filename": final,
            "size": len(data),
            "uploaded_at": _now(),
            "section": section,
        })
    _persist(rec)
    return rec, skipped


def set_section_text(pid: str, section: str, text: str) -> dict:
    """Store/clear the free-text entry for a section (e.g. qualifications)."""
    rec = get_project(pid)
    if not rec:
        raise KeyError(pid)
    st = dict(rec.get("section_text", {}))
    text = (text or "").strip()
    if text:
        st[section] = text
    else:
        st.pop(section, None)
    rec["section_text"] = st
    return _persist(rec)


def remove_document(pid: str, filename: str) -> dict:
    rec = get_project(pid)
    if not rec:
        raise KeyError(pid)
    rec["documents"] = [d for d in rec["documents"] if d["filename"] != filename]
    fpath = local_store.uploads_dir() / pid / _safe_filename(filename)
    try:
        fpath.unlink(missing_ok=True)
    except OSError:
        pass
    return _persist(rec)


def document_path(pid: str, filename: str) -> Path | None:
    fpath = local_store.uploads_dir() / pid / _safe_filename(filename)
    return fpath if fpath.exists() else None


# --------------------------------------------------------------------------- #
# Context answers                                                             #
# --------------------------------------------------------------------------- #

def save_answers(pid: str, posted: dict[str, object]) -> dict:
    """Merge posted answers (keyed by qid) into the record, ignoring unknown
    qids and blank values. Multi-select arrives as a list."""
    rec = get_project(pid)
    if not rec:
        raise KeyError(pid)
    valid = question_map.valid_qids()
    answers = dict(rec.get("answers", {}))
    for qid in valid:
        if qid not in posted:
            continue  # field not in this submit — leave existing untouched
        val = posted[qid]
        if _has_value(val):
            answers[qid] = val
        else:
            answers.pop(qid, None)  # cleared
    rec["answers"] = answers
    return _persist(rec)


def answered_count(rec: dict) -> int:
    return sum(1 for v in rec.get("answers", {}).values() if _has_value(v))


# --------------------------------------------------------------------------- #
# Job parameters (markup / waste)                                             #
# --------------------------------------------------------------------------- #

def _parse_pct(raw) -> float | None:
    """Parse a percentage input to a float, or None if blank/invalid.
    Accepts '12', '12.5', '12%'. Clamps to a sane 0–100 range."""
    if raw is None:
        return None
    s = str(raw).strip().rstrip("%").strip()
    if s == "":
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return max(0.0, min(v, 100.0))


def set_job_params(pid: str, markup_pct, waste_pct) -> dict:
    """Store the profit markup % and waste factor %. Blank clears the value."""
    rec = get_project(pid)
    if not rec:
        raise KeyError(pid)
    rec["markup_pct"] = _parse_pct(markup_pct)
    rec["waste_pct"] = _parse_pct(waste_pct)
    return _persist(rec)


# --------------------------------------------------------------------------- #
# Context document compilation ("Generate the estimate" — step 1)             #
# --------------------------------------------------------------------------- #

def _fmt_pct(v) -> str:
    return f"{v:g}%" if isinstance(v, (int, float)) else "not set"


def build_context_document(rec: dict) -> tuple[str, bytes]:
    """Compile every piece of captured context into one Markdown document.

    Returns (filename, bytes). This is the single artefact the estimation
    workflow consumes; it is attached back to the project's documents so the
    estimator can see exactly what was handed to the estimate.
    """
    import datetime as _dt

    idx = question_map.question_index()
    L: list[str] = []
    a = L.append

    a(f"# Project Context Summary — {rec.get('name', 'Untitled project')}")
    a("")
    a(f"*Generated {_dt.datetime.now().strftime('%d %b %Y, %H:%M')}*")
    a("")
    a("## Project")
    a(f"- **Name:** {rec.get('name', '')}")
    if rec.get("client"):
        a(f"- **Client:** {rec['client']}")
    if rec.get("reference"):
        a(f"- **Reference:** {rec['reference']}")
    a("")
    a("## Job parameters")
    a(f"- **Profit markup:** {_fmt_pct(rec.get('markup_pct'))}")
    a(f"- **Waste factor:** {_fmt_pct(rec.get('waste_pct'))}")
    a("")

    # Qualifications.
    qual_text = section_text(rec, SECTION_QUALIFICATIONS)
    qual_docs = documents_in(rec, SECTION_QUALIFICATIONS)
    a("## Qualifications")
    if qual_text:
        a(qual_text)
    if qual_docs:
        a("")
        a("Attached qualification documents:")
        for d in qual_docs:
            a(f"- {d['filename']}")
    if not qual_text and not qual_docs:
        a("_None provided._")
    a("")

    # Roofing context answers, grouped by element group → sub-element.
    a("## Roofing context")
    answers = rec.get("answers", {})
    grouped: dict[str, dict[str, list[tuple[dict, object]]]] = {}
    order: list[str] = []
    for qid, val in answers.items():
        if qid not in idx or not _has_value(val):
            continue
        q = idx[qid]
        g, sub = q["group"], q["subelement"]
        if g not in grouped:
            grouped[g] = {}
            order.append(g)
        grouped[g].setdefault(sub, []).append((q, val))
    if not order:
        a("_No context questions answered yet._")
    for g in order:
        a("")
        a(f"### {g}")
        for sub, items in grouped[g].items():
            a(f"**{sub}**")
            a("")
            for q, val in items:
                if isinstance(val, (list, tuple)):
                    ans = ", ".join(str(x) for x in val)
                else:
                    ans = str(val)
                unit = f" {q['unit']}" if q.get("unit") else ""
                a(f"- {q['question']} — **{ans}{unit}**  ")
                a(f"  _({q['qid']}; feeds {q.get('feeds_step', '')})_")
            a("")

    # Reference lists of attached documents.
    proj_docs = [d for d in documents_in(rec, SECTION_PROJECT)
                 if d["filename"] != GENERATED_CONTEXT_NAME]
    a("## Project documents")
    if proj_docs:
        for d in proj_docs:
            a(f"- {d['filename']}")
    else:
        a("_None uploaded._")
    a("")
    for sect, label in ((SECTION_JOB_TERMS, "Job terms & conditions"),
                        (SECTION_CLIENT_TERMS, "Client terms & conditions")):
        docs = documents_in(rec, sect)
        a(f"## {label}")
        if docs:
            for d in docs:
                a(f"- {d['filename']}")
        else:
            a("_None uploaded._")
        a("")

    text = "\n".join(L).rstrip() + "\n"
    return GENERATED_CONTEXT_NAME, text.encode("utf-8")


def generate_estimate(pid: str) -> tuple[dict, str]:
    """Step 1 of the estimate workflow: compile all project context into a
    single document and attach it to the project's uploaded documents
    (replacing any previously generated copy). Returns (record, filename).

    Later workflow steps will be triggered from here.
    """
    rec = get_project(pid)
    if not rec:
        raise KeyError(pid)

    filename, data = build_context_document(rec)

    # Remove any previous generated summary (file + metadata) so we don't
    # accumulate duplicates on re-generation.
    rec["documents"] = [d for d in rec["documents"]
                        if d.get("filename") != filename]
    fpath = local_store.uploads_dir() / pid / _safe_filename(filename)
    try:
        fpath.unlink(missing_ok=True)
    except OSError:
        pass

    pdir = local_store.uploads_dir() / pid
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / _safe_filename(filename)).write_bytes(data)
    rec["documents"].append({
        "filename": filename,
        "size": len(data),
        "uploaded_at": _now(),
        "section": SECTION_PROJECT,
        "generated": True,
    })
    _persist(rec)
    return rec, filename


def context_export(rec: dict) -> dict:
    """Flatten answers into an estimation-ready structure: only answered
    questions, each with its full question-map metadata. This is what a later
    estimation stage would consume."""
    idx = question_map.question_index()
    items = []
    for qid, val in rec.get("answers", {}).items():
        if qid not in idx or not _has_value(val):
            continue
        q = idx[qid]
        items.append({
            "qid": qid,
            "group": q["group"],
            "subelement": q["subelement"],
            "question": q["question"],
            "answer": val,
            "unit": q.get("unit", ""),
            "feeds_step": q.get("feeds_step", ""),
            "source_doc": q.get("source_doc", ""),
        })
    return {
        "project": {
            "id": rec["id"],
            "name": rec.get("name", ""),
            "client": rec.get("client", ""),
            "reference": rec.get("reference", ""),
            "markup_pct": rec.get("markup_pct"),
            "waste_pct": rec.get("waste_pct"),
        },
        "documents": documents_in(rec, SECTION_PROJECT),
        "qualifications": {
            "text": section_text(rec, SECTION_QUALIFICATIONS),
            "documents": documents_in(rec, SECTION_QUALIFICATIONS),
        },
        "job_terms": {
            "documents": documents_in(rec, SECTION_JOB_TERMS),
        },
        "client_terms": {
            "documents": documents_in(rec, SECTION_CLIENT_TERMS),
        },
        "context": items,
        "n_context_answers": len(items),
    }
