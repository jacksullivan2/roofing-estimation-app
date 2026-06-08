"""FastAPI app shell for the Roofing Estimator.

Responsibilities:
- App lifecycle (ensure data dir, warm the question map cache).
- Public routes: /, /login, /logout, /healthz.
- Mount feature routers (projects today; pricing/estimation later).

All feature work lives under app/features/<feature>/. Nothing here knows
about a feature's internals.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, sessions, settings, question_map
from .infra import local_store
from .features.projects.routes import router as projects_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger("roofing")

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_dir = local_store.ensure_dir()
    LOGGER.info("Persistent data dir: %s", data_dir)
    qm = question_map.load()
    LOGGER.info("Loaded question map: %s groups, %s questions",
                qm.get("n_groups"), qm.get("n_questions"))
    if not settings.auth_enabled():
        LOGGER.info("ADMIN_PASSWORD not set — running in OPEN mode (no login).")
    yield


app = FastAPI(title="Roofing Estimator", docs_url=None, redoc_url=None,
              lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(auth.RedirectException)
async def _redirect_handler(request: Request, exc: auth.RedirectException):
    return RedirectResponse(exc.location, status_code=303)


# Projects feature: /projects/*
app.include_router(projects_router)


# --------------------------------------------------------------------------- #
# Public routes                                                                #
# --------------------------------------------------------------------------- #

@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    if not settings.auth_enabled():
        return RedirectResponse("/projects", status_code=303)
    sid = request.cookies.get(settings.SESSION_COOKIE_NAME)
    sess = sessions.get_session(sid)
    if sess and sess.get("authed"):
        return RedirectResponse("/projects", status_code=303)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None):
    if not settings.auth_enabled():
        return RedirectResponse("/projects", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
def login_submit(request: Request, password: str = Form(...)):
    if not auth.password_matches(password):
        return RedirectResponse("/login?error=Wrong+password", status_code=303)
    sid = sessions.new_session()
    sessions.set_session_value(sid, "authed", True)
    resp = RedirectResponse("/projects", status_code=303)
    resp.set_cookie(
        settings.SESSION_COOKIE_NAME, sid,
        httponly=True, samesite="lax",
        secure=settings.COOKIE_SECURE,
        max_age=settings.SESSION_TTL_SECONDS,
    )
    return resp


@app.post("/logout")
def logout(request: Request):
    sid = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if sid:
        sessions.drop_session(sid)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(settings.SESSION_COOKIE_NAME)
    return resp


@app.get("/healthz", response_class=HTMLResponse)
def healthz():
    """Cheap probe for container health checks. Returns 'ok'."""
    return HTMLResponse("ok")
