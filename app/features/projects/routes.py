"""FastAPI router for the Projects feature. Mounted at /projects.

Routes:
    GET   /projects                         list + create form
    POST  /projects                         create a project -> redirect to detail
    GET   /projects/{id}                     detail: upload docs + context accordion
    POST  /projects/{id}/documents           upload one or more documents (HTMX)
    POST  /projects/{id}/documents/delete     remove one document (HTMX)
    GET   /projects/{id}/documents/{name}     download a document
    POST  /projects/{id}/context              save context answers (HTMX)
    GET   /projects/{id}/export.json          estimation-ready context export
    POST  /projects/{id}/delete               delete a project
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app import auth, question_map, sessions, settings
from . import core, tender

LOGGER = logging.getLogger(__name__)

_SHARED_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"
_FEATURE_TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(
    directory=[str(_FEATURE_TEMPLATE_DIR), str(_SHARED_TEMPLATE_DIR)]
)
# Make auth state available to every template (nav uses it).
templates.env.globals["auth_enabled"] = settings.auth_enabled()

router = APIRouter(prefix="/projects", tags=["projects"])


def _qs(s: str) -> str:
    return urllib.parse.quote(s[:300])


def _human_size(n: int) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _ts(epoch) -> str:
    import datetime as _dt
    try:
        return _dt.datetime.fromtimestamp(float(epoch)).strftime("%d %b %Y, %H:%M")
    except (TypeError, ValueError):
        return ""


templates.env.filters["human_size"] = _human_size
templates.env.filters["ts"] = _ts


# --------------------------------------------------------------------------- #
# List + create                                                               #
# --------------------------------------------------------------------------- #

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def list_page(request: Request, sid: str = Depends(auth.require_login),
              error: str | None = None):
    projects = core.list_projects()
    return templates.TemplateResponse(request, "projects_list.html", {
        "active": "projects",
        "projects": projects,
        "error": error,
        "total_questions": question_map.total_questions(),
    })


@router.post("")
@router.post("/")
def create_submit(request: Request, sid: str = Depends(auth.require_login),
                  name: str = Form(...), client: str = Form(""),
                  reference: str = Form("")):
    if not (name or "").strip():
        return RedirectResponse("/projects?error=Project+name+is+required",
                                status_code=303)
    rec = core.create_project(name=name, client=client, reference=reference)
    return RedirectResponse(f"/projects/{rec['id']}", status_code=303)


# --------------------------------------------------------------------------- #
# Detail                                                                       #
# --------------------------------------------------------------------------- #

@router.get("/{pid}", response_class=HTMLResponse)
def detail_page(pid: str, request: Request,
                sid: str = Depends(auth.require_login)):
    rec = core.get_project(pid)
    if not rec:
        raise HTTPException(404, "Project not found")
    return templates.TemplateResponse(request, "project_detail.html", {
        "active": "projects",
        "project": rec,
        "groups": question_map.groups(),
        "answers": rec.get("answers", {}),
        "answered": core.answered_count(rec),
        "total_questions": question_map.total_questions(),
    })


# --------------------------------------------------------------------------- #
# Documents                                                                    #
# --------------------------------------------------------------------------- #

def _docs_fragment(request: Request, rec: dict, section: str, **extra):
    return templates.TemplateResponse(request, "_documents.html", {
        "project": rec,
        "section": section,
        "docs": core.documents_in(rec, section),
        **extra,
    })


@router.post("/{pid}/documents", response_class=HTMLResponse)
async def upload_documents(pid: str, request: Request,
                           sid: str = Depends(auth.require_login),
                           files: list[UploadFile] = File(default=[]),
                           section: str = Form(core.SECTION_PROJECT)):
    rec = core.get_project(pid)
    if not rec:
        raise HTTPException(404)

    payloads: list[tuple[str, bytes]] = []
    total = 0
    for uf in files:
        name = (uf.filename or "").strip()
        if not name:
            continue
        data = await uf.read()
        total += len(data)
        if total > settings.MAX_UPLOAD_BYTES:
            mb = settings.MAX_UPLOAD_BYTES // (1024 * 1024)
            return _docs_fragment(request, rec, section,
                                  flash_error=f"Upload exceeds {mb} MB limit")
        payloads.append((name, data))

    skipped: list[str] = []
    if payloads:
        rec, skipped = core.add_documents(pid, payloads, section=section)

    return _docs_fragment(
        request, rec, section,
        flash_ok=f"Added {len(payloads) - len(skipped)} document(s)" if payloads else None,
        skipped=skipped,
    )


@router.post("/{pid}/documents/delete", response_class=HTMLResponse)
def delete_document(pid: str, request: Request,
                    sid: str = Depends(auth.require_login),
                    filename: str = Form(...),
                    section: str = Form(core.SECTION_PROJECT)):
    rec = core.get_project(pid)
    if not rec:
        raise HTTPException(404)
    rec = core.remove_document(pid, filename)
    return _docs_fragment(request, rec, section, flash_ok="Document removed")


@router.post("/{pid}/section-text", response_class=HTMLResponse)
def save_section_text(pid: str, request: Request,
                      sid: str = Depends(auth.require_login),
                      section: str = Form(...), text: str = Form("")):
    rec = core.get_project(pid)
    if not rec:
        raise HTTPException(404)
    core.set_section_text(pid, section, text)
    return HTMLResponse(
        '<span class="text-xs text-emerald-700 bg-emerald-50 border '
        'border-emerald-200 rounded-full px-2.5 py-1">Saved</span>'
    )


@router.get("/{pid}/documents/{filename}")
def download_document(pid: str, filename: str,
                      sid: str = Depends(auth.require_login)):
    path = core.document_path(pid, filename)
    if not path:
        raise HTTPException(404)
    return Response(
        content=path.read_bytes(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


# --------------------------------------------------------------------------- #
# Context answers                                                              #
# --------------------------------------------------------------------------- #

def _collapse(form) -> dict[str, object]:
    """Collapse a form multidict: multi-selects produce repeated keys -> list."""
    posted: dict[str, object] = {}
    for key in form.keys():
        values = form.getlist(key)
        posted[key] = values if len(values) > 1 else values[0]
    return posted


def _save_form(pid: str, posted: dict) -> dict:
    """Persist answers plus the job parameters (markup / waste) from a submit."""
    core.set_job_params(pid, posted.get("markup_pct"), posted.get("waste_pct"))
    return core.save_answers(pid, posted)


@router.post("/{pid}/context", response_class=HTMLResponse)
async def save_context(pid: str, request: Request,
                       sid: str = Depends(auth.require_login)):
    rec = core.get_project(pid)
    if not rec:
        raise HTTPException(404)
    rec = _save_form(pid, _collapse(await request.form()))
    return templates.TemplateResponse(request, "_save_status.html", {
        "answered": core.answered_count(rec),
        "total_questions": question_map.total_questions(),
        "saved": True,
    })


@router.post("/{pid}/generate", response_class=HTMLResponse)
async def generate_estimate(pid: str, request: Request,
                            sid: str = Depends(auth.require_login)):
    """Save the latest context, then compile it all into one document and
    attach it to the project's uploaded documents (workflow step 1)."""
    rec = core.get_project(pid)
    if not rec:
        raise HTTPException(404)
    _save_form(pid, _collapse(await request.form()))
    rec, filename = core.generate_estimate(pid)
    return templates.TemplateResponse(request, "_generate_result.html", {
        "project": rec,
        "filename": filename,
        "section": core.SECTION_PROJECT,
        "docs": core.documents_in(rec, core.SECTION_PROJECT),
    })


# --------------------------------------------------------------------------- #
# Estimate tender workflow                                                     #
# --------------------------------------------------------------------------- #

@router.post("/{pid}/tender", response_class=HTMLResponse)
async def start_tender(pid: str, request: Request,
                       sid: str = Depends(auth.require_login)):
    """Save the latest context, then kick off the background tender workflow
    and return the status panel (which polls itself until complete)."""
    rec = core.get_project(pid)
    if not rec:
        raise HTTPException(404)
    _save_form(pid, _collapse(await request.form()))
    job = sessions.create_tender_job(pid)
    tender.start(job.job_id)
    return templates.TemplateResponse(request, "_tender_status.html", {
        "project": rec, "job": job,
    })


@router.get("/{pid}/tender/{job_id}/status", response_class=HTMLResponse)
def tender_status(pid: str, job_id: str, request: Request,
                  sid: str = Depends(auth.require_login)):
    job = sessions.get_tender_job(job_id)
    if not job or job.project_id != pid:
        raise HTTPException(404)
    ctx = {"project": core.get_project(pid), "job": job}
    if job.status == "done":
        ctx["section"] = core.SECTION_PROJECT
        ctx["docs"] = core.documents_in(ctx["project"], core.SECTION_PROJECT)
    return templates.TemplateResponse(request, "_tender_status.html", ctx)


@router.get("/{pid}/tender/{job_id}/download/{which}")
def tender_download(pid: str, job_id: str, which: str,
                    sid: str = Depends(auth.require_login)):
    job = sessions.get_tender_job(job_id)
    if not job or job.project_id != pid or job.status != "done":
        raise HTTPException(404)
    if which == "pricing" and job.pricing_bytes is not None:
        data, name, media = job.pricing_bytes, job.pricing_filename, job.pricing_media_type
    elif which == "tender" and job.tender_bytes is not None:
        data, name, media = job.tender_bytes, job.tender_filename, job.tender_media_type
    else:
        raise HTTPException(404)
    return Response(content=data, media_type=media or "application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/{pid}/export.json")
def export_context(pid: str, sid: str = Depends(auth.require_login)):
    rec = core.get_project(pid)
    if not rec:
        raise HTTPException(404)
    payload = core.context_export(rec)
    body = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    fname = f"context_{rec.get('reference') or rec['id']}.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/{pid}/delete")
def delete_project_route(pid: str, sid: str = Depends(auth.require_login)):
    core.delete_project(pid)
    return RedirectResponse("/projects", status_code=303)
