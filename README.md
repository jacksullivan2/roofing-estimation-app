# Roofing Estimator — project intake & context

A local web app for roofing estimators to (1) upload project documents and
(2) supplement them with roofing-specific context, driven by the **Roofing
Estimation Question Map**. The captured context is exportable as JSON, ready
to feed a downstream estimation pipeline.

Tech Stack: **FastAPI + HTMX + Tailwind (CDN)**, Jinja2
templates with feature-based routers, in-memory sessions, atomic JSON
persistence, and a single-image Docker container on uvicorn `:8000`.

---

## What it does

**1. Project documents** — upload condition reports, schedules of works,
specifications, drawings, photos and price documents. Files are stored on a
persistent volume; you can download or remove them.

**2. Roofing context** — the Question Map (17 element groups, 78 questions)
is rendered as a two-level accordion:

- Each **element group** (e.g. *Balcony / Terrace / Walkway*) is a dropdown.
- Inside, each **element / sub-element** is a dropdown that, when opened,
  reveals its question(s) with an input box.
- Inputs adapt to the question's data type — a dropdown for single-select
  questions (using the Question Map's allowed values), checkboxes for
  multi-select, a number box for quantities, and a text box otherwise. Each
  question shows its purpose, the estimation step it feeds, and its source
  manufacturer document.

Answers save over HTMX (no page reload) and progress is tracked
(`answered / total`).

The context section is bookended by three dedicated areas:

- **Qualifications** (top) — capture job qualifications, assumptions or
  exclusions as free text and/or by uploading a document.
- **Job terms & conditions** and **Client terms & conditions** (bottom) —
  upload the relevant T&C documents for each.

Export everything — answers, qualifications, and both T&C sets — with
**Export context (JSON)**.

**Job parameters** — a card at the top of the project captures the **profit
markup (%)** and **waste factor (%)** to apply to the job. They save with the
context and appear in the export and the compiled document.

**Two actions** at the bottom of the context section:

- **Save context** — persist everything so you can return and finish later.
- **Generate the estimate** — saves first, then compiles all project context
  (job parameters, qualifications, every answered question, and the document
  references) into a single Markdown document and attaches it to the project's
  uploaded documents. Re-generating replaces the previous summary. This is
  step 1 of the estimate workflow; later steps will build on this artefact.

---

## Run it (Docker)

```bash
cd roofing_estimator_app

# Option A: docker compose (recommended — creates a named data volume)
docker compose up --build
# open http://localhost:8000

# Option B: plain docker
docker build -t roofing-estimator:dev .
docker run --rm -p 8000:8000 -v roofing_data:/home/data roofing-estimator:dev
```

Open <http://localhost:8000>. By default there is **no login** (open local
mode). To require a password, set `ADMIN_PASSWORD` (uncomment it in
`docker-compose.yml` or pass `-e ADMIN_PASSWORD=...`).

### Run without Docker (dev)

```bash
pip install -r requirements.txt
DATA_DIR=./data uvicorn app.main:app --reload --port 8000
```

---

## Project layout

```
app/
  main.py                  FastAPI shell: /, /login, /logout, /healthz; mounts routers
  settings.py              env-driven config (optional auth, upload limits)
  auth.py / sessions.py    optional login gate + in-memory sessions
  version.py / __init__.py app version injected into every template
  question_map.py          loads app/data/question_map.json
  data/question_map.json   the Question Map (groups → sub-elements → questions)
  infra/local_store.py     atomic JSON store + uploads dir on the data volume
  features/projects/
    routes.py              /projects/* routes (list, detail, upload, context, export)
    core.py                project persistence + document handling
    templates/             projects_list, project_detail, _documents, _save_status
  templates/               base.html, _nav.html, login.html (shared)
  static/                  favicon
```

## Data & persistence

Everything lives under `DATA_DIR` (default `/home/data`, a Docker volume):

| Path | Contents |
| --- | --- |
| `projects.json` | index of all projects (summary fields) |
| `project_<id>.json` | full record: metadata + documents + answers |
| `uploads/<id>/` | the raw uploaded files |

All JSON writes are atomic (temp file + rename) so a crash never corrupts a
record.

## Updating the question set

The UI is fully driven by `app/data/question_map.json`. Regenerate it from
the Question Map workbook and restart the container — no code changes needed.

## Health check

`GET /healthz` returns `ok` for container health probes.
