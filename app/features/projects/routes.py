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
from app.infra import s3_config
from . import core, labour_rates, repo, tender

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


def _numg(v) -> str:
    """Tidy numeric display: 22.0 -> '22', 12.5 -> '12.5', None -> ''."""
    if isinstance(v, (int, float)):
        return f"{v:g}"
    return ""


templates.env.filters["human_size"] = _human_size
templates.env.filters["ts"] = _ts
templates.env.filters["numg"] = _numg


# --------------------------------------------------------------------------- #
# List + create                                                               #
# --------------------------------------------------------------------------- #

def _storage_ctx(status: str | None = None, ok: bool | None = None) -> dict:
    cfg = s3_config.get()
    return {
        "s3": {
            "enabled": cfg.get("enabled"),
            "bucket": cfg.get("bucket", ""),
            "region": cfg.get("region", ""),
            "access_key_id": cfg.get("access_key_id", ""),
            "has_secret": bool(cfg.get("secret_access_key")),
            "boto3": s3_config.boto3_available(),
        },
        "storage_mode": repo.storage_mode(),
        "storage_status": status,
        "storage_ok": ok,
    }


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def list_page(request: Request, sid: str = Depends(auth.require_login),
              error: str | None = None, s3msg: str | None = None,
              s3ok: str | None = None):
    projects = core.list_projects()
    ctx = {
        "active": "projects",
        "projects": projects,
        "error": error,
        "total_questions": question_map.total_questions(),
    }
    ok = None if s3ok is None else (s3ok == "1")
    ctx.update(_storage_ctx(status=s3msg, ok=ok))
    ctx["labour_files"] = labour_rates.list_library()
    return templates.TemplateResponse(request, "projects_list.html", ctx)


@router.post("/storage")
def save_storage(request: Request, sid: str = Depends(auth.require_login),
                 bucket: str = Form(""), region: str = Form(""),
                 access_key_id: str = Form(""),
                 secret_access_key: str = Form(""),
                 enabled: str = Form("on")):
    if not bucket.strip():
        return RedirectResponse(
            "/projects?s3ok=0&s3msg=" + _qs("Enter a bucket name."), status_code=303)
    cfg = s3_config.save(
        bucket=bucket, region=region,
        access_key_id=access_key_id, secret_access_key=secret_access_key,
        enabled=bool(enabled),
    )
    ok, msg = s3_config.test_connection(cfg)
    return RedirectResponse(
        f"/projects?s3ok={'1' if ok else '0'}&s3msg=" + _qs(msg), status_code=303)


@router.post("/storage/test")
def test_storage(request: Request, sid: str = Depends(auth.require_login)):
    ok, msg = s3_config.test_connection()
    return RedirectResponse(
        f"/projects?s3ok={'1' if ok else '0'}&s3msg=" + _qs(msg), status_code=303)


@router.post("/storage/disconnect")
def disconnect_storage(request: Request, sid: str = Depends(auth.require_login)):
    s3_config.clear()
    return RedirectResponse(
        "/projects?s3ok=1&s3msg=" + _qs("Disconnected — using local storage."),
        status_code=303)


# --- Labour-rates library (must be registered before /{pid}) ---------------- #

@router.post("/labour-rates")
async def labour_rates_upload(request: Request,
                              sid: str = Depends(auth.require_login),
                              files: list[UploadFile] = File(default=[])):
    n = 0
    for uf in files:
        name = (uf.filename or "").strip()
        data = await uf.read()
        if not name or not data:
            continue
        if not core.ext_allowed(name):
            return RedirectResponse(
                "/projects?s3ok=0&s3msg=" + _qs(f"{name}: unsupported file type"),
                status_code=303)
        labour_rates.upload_to_library(name, data)
        n += 1
    msg = f"Added {n} labour-rates document(s) to the library." if n else "No file selected."
    return RedirectResponse(f"/projects?s3ok={'1' if n else '0'}&s3msg=" + _qs(msg),
                            status_code=303)


@router.get("/labour-rates/{filename}")
def labour_rates_download(filename: str,
                          sid: str = Depends(auth.require_login)):
    data = labour_rates.read_library(filename)
    if data is None:
        raise HTTPException(404)
    return Response(content=data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


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
    answers = rec.get("answers", {})
    groups = question_map.groups()
    # Groups that already hold data — pre-checked in the optional-context
    # checklist so returning users see their sections open.
    answered_groups = set()
    for g in groups:
        for sub in g.get("subelements", []):
            if any(q["qid"] in answers for q in sub.get("questions", [])):
                answered_groups.add(g["group_id"])
                break
    qual_active = bool(core.section_text(rec, core.SECTION_QUALIFICATIONS)
                       or core.documents_in(rec, core.SECTION_QUALIFICATIONS))
    return templates.TemplateResponse(request, "project_detail.html", {
        "active": "projects",
        "project": rec,
        "groups": groups,
        "answers": answers,
        "answered": core.answered_count(rec),
        "total_questions": question_map.total_questions(),
        "answered_groups": answered_groups,
        "qual_active": qual_active,
        "draft": core.draft_state(rec),
        "labour_has_project": labour_rates.project_has_labour_rates(rec),
        "labour_latest": labour_rates.latest_library_meta(),
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
    data = core.read_document_bytes(pid, filename)
    if data is None:
        raise HTTPException(404)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
    """Persist a (possibly partial) submit. Job params are only written when the
    fields are present, so a per-section save never wipes markup/waste; answers
    are merged by qid, so unrelated sections are left untouched.

    Client pricing sheet: checkboxes don't submit when unchecked, so the form
    carries a hidden marker `client_pricing_present`. Only when the marker is
    in the submit do we update the flag (checked state + selected file)."""
    if "markup_pct" in posted or "waste_pct" in posted:
        core.set_job_params(pid, posted.get("markup_pct"), posted.get("waste_pct"))
    if "client_pricing_present" in posted:
        core.set_client_pricing(
            pid,
            enabled=("client_pricing_enabled" in posted),
            filename=str(posted.get("client_pricing_filename", "") or ""),
        )
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


# --------------------------------------------------------------------------- #
# Estimate workflow (two passes: draft items -> qualifications -> final)       #
# --------------------------------------------------------------------------- #

def _draft_item_ctx(rec: dict) -> dict:
    d = core.draft_state(rec)
    return {
        "project": rec,
        "draft": d,
        "item": core.current_draft_item(rec),
        "n_items": len(d.get("items", [])),
        "n_qualified": sum(1 for i in d.get("items", []) if i.get("qualification")),
    }


@router.post("/{pid}/draft", response_class=HTMLResponse)
async def start_draft(pid: str, request: Request,
                      sid: str = Depends(auth.require_login)):
    """Save the latest inputs, then generate the DRAFT pricing items in the
    background. The status panel polls and then opens the item review loop."""
    rec = core.get_project(pid)
    if not rec:
        raise HTTPException(404)
    _save_form(pid, _collapse(await request.form()))
    job = sessions.create_tender_job(pid, kind="draft")
    tender.start(job.job_id)
    return templates.TemplateResponse(request, "_draft_status.html", {
        "project": rec, "job": job,
    })


@router.get("/{pid}/draft/status/{job_id}", response_class=HTMLResponse)
def draft_status(pid: str, job_id: str, request: Request,
                 sid: str = Depends(auth.require_login)):
    job = sessions.get_tender_job(job_id)
    if not job or job.project_id != pid or job.kind != "draft":
        raise HTTPException(404)
    rec = core.get_project(pid)
    if job.status == "done":
        return templates.TemplateResponse(request, "_draft_item.html",
                                          _draft_item_ctx(rec))
    return templates.TemplateResponse(request, "_draft_status.html", {
        "project": rec, "job": job,
    })


@router.get("/{pid}/draft/item", response_class=HTMLResponse)
def draft_item(pid: str, request: Request,
               sid: str = Depends(auth.require_login)):
    rec = core.get_project(pid)
    if not rec:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "_draft_item.html",
                                      _draft_item_ctx(rec))


@router.post("/{pid}/draft/qualify", response_class=HTMLResponse)
def draft_qualify(pid: str, request: Request,
                  sid: str = Depends(auth.require_login),
                  qualification: str = Form(""),
                  action: str = Form("submit")):
    """Record the qualification (or skip) for the current item, advance, and
    return the next item — or the completion panel."""
    rec = core.get_project(pid)
    if not rec:
        raise HTTPException(404)
    rec = core.qualify_current_item(pid, qualification, skipped=(action == "skip"))
    return templates.TemplateResponse(request, "_draft_item.html",
                                      _draft_item_ctx(rec))


@router.post("/{pid}/draft/review-again", response_class=HTMLResponse)
def draft_review_again(pid: str, request: Request,
                       sid: str = Depends(auth.require_login)):
    rec = core.reopen_draft_review(pid)
    return templates.TemplateResponse(request, "_draft_item.html",
                                      _draft_item_ctx(rec))


@router.post("/{pid}/tender", response_class=HTMLResponse)
async def start_tender(pid: str, request: Request,
                       sid: str = Depends(auth.require_login)):
    """FINAL pass: resubmit the pricing sheet with qualifications — generates
    the final pricing sheet + tender in the background."""
    rec = core.get_project(pid)
    if not rec:
        raise HTTPException(404)
    _save_form(pid, _collapse(await request.form()))
    job = sessions.create_tender_job(pid, kind="final")
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
