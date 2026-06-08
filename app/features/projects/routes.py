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

from app import auth, question_map, settings
from . import core

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

@router.post("/{pid}/documents", response_class=HTMLResponse)
async def upload_documents(pid: str, request: Request,
                           sid: str = Depends(auth.require_login),
                           files: list[UploadFile] = File(default=[])):
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
            return templates.TemplateResponse(request, "_documents.html", {
                "project": rec,
                "flash_error": f"Upload exceeds {mb} MB limit",
            })
        payloads.append((name, data))

    skipped: list[str] = []
    if payloads:
        rec, skipped = core.add_documents(pid, payloads)

    return templates.TemplateResponse(request, "_documents.html", {
        "project": rec,
        "flash_ok": f"Added {len(payloads) - len(skipped)} document(s)" if payloads else None,
        "skipped": skipped,
    })


@router.post("/{pid}/documents/delete", response_class=HTMLResponse)
def delete_document(pid: str, request: Request,
                    sid: str = Depends(auth.require_login),
                    filename: str = Form(...)):
    rec = core.get_project(pid)
    if not rec:
        raise HTTPException(404)
    rec = core.remove_document(pid, filename)
    return templates.TemplateResponse(request, "_documents.html", {
        "project": rec,
        "flash_ok": "Document removed",
    })


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

@router.post("/{pid}/context", response_class=HTMLResponse)
async def save_context(pid: str, request: Request,
                       sid: str = Depends(auth.require_login)):
    rec = core.get_project(pid)
    if not rec:
        raise HTTPException(404)
    form = await request.form()
    # Collapse the multidict: multi-selects produce repeated keys -> list.
    posted: dict[str, object] = {}
    for key in form.keys():
        values = form.getlist(key)
        posted[key] = values if len(values) > 1 else values[0]
    rec = core.save_answers(pid, posted)
    return templates.TemplateResponse(request, "_save_status.html", {
        "answered": core.answered_count(rec),
        "total_questions": question_map.total_questions(),
        "saved": True,
    })


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
