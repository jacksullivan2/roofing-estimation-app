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

The project page is a three-step flow:

**Step 1 — Upload & context.** Upload project documents and both T&C sets, set
the job parameters (markup/waste %), and optionally add context: a checklist
lists Qualifications plus every Question Map topic — ticking one opens that
section's questions inline. Sections already holding data come pre-ticked.

**Step 2 — Draft pricing review.** Generate the draft pricing sheet; its line
items are returned to the UI one at a time. For each item add qualifying
information and submit (or skip = no qualifications), and the next item
appears. You can re-walk the items at any time.

**Step 3 — Final documents.** Resubmit the pricing sheet with the item
qualifications — the full workflow runs (context doc → S3 prompts → AI model)
and returns the final pricing sheet (with a Qualifications column) and the
tender document (with an "Item qualifications" section) for download.

The previous four-tab reference (below) is superseded by this flow:

**Project Files** — upload condition reports, schedules of works,
specifications, drawings, photos and price documents; plus a **Terms &
conditions** area combining the Job and Client T&C uploads. Files are stored in
the project's folder; you can download or remove them.

**Project Context** — the Question Map (17 element groups, 78 questions) as a
set of sub-tabs down the left: **Qualifications** first, then one sub-tab per
element group (*Project & Global Context*, *Substrate & Priming*, …). Opening a
sub-tab shows just that section's questions. Inputs adapt to the question's data
type — a dropdown for single-select (using the Question Map's allowed values),
checkboxes for multi-select, a number box for quantities, a text box otherwise
— and each shows its purpose, the estimation step it feeds, and its source
document. Qualifications captures free text and/or an uploaded document. Each
sub-tab has its own **Save**.

**Job Parameters** — the **profit markup (%)** and **waste factor (%)** applied
across the estimate, with a **Save**.

**Estimate** — the **Save** and **Estimate** actions. Save writes everything to
the project's storage folder so you can return later; the green **Estimate**
button runs the full workflow below and produces the pricing sheet + tender.

Every Save persists to the project's folder (S3 or local); a save from one tab
never disturbs data entered in another. Export everything — answers,
qualifications, and both T&C sets — with **Export context (JSON)**.

## Estimate workflow

Clicking **Estimate** runs these steps in a background job, with a live status
panel, then offers the pricing sheet + tender for download:

1. Compile all project context (including profit & waste %) into
   *Project Context &lt;name&gt;* and attach it to the uploaded project documents.
2. Retrieve the workflow-step markdown files from **AWS S3** — falling back to
   the bundled `_agent_prompts/` folder when S3 isn't configured.
3. Pass those steps + the project documents to an **AI model** to generate the
   **pricing sheet** (`.xlsx`) and the **tender document**.
4. Return both to the front end for download.

### Configuring S3 + the AI model

These are optional — see `.env.example`. **Until they're set the workflow still
runs end to end**: prompts come from the local `_agent_prompts/` folder and the
AI step produces clearly-labelled *placeholder* pricing/tender outputs, so the
plumbing is testable today.

| Concern | Where to wire it |
| --- | --- |
| S3 location for prompts | `AGENT_PROMPTS_S3_BUCKET` / `AGENT_PROMPTS_S3_PREFIX` / `AWS_REGION` + standard AWS creds → `app/infra/s3_client.py` |
| AI provider & keys | `AI_PROVIDER` (`bedrock`\|`openai`\|`http`) + the matching vars below → `app/infra/ai_client.py` |

Two fully-implemented providers run the **same step workflow** (steps 01–09
draft, 10–11 final, shared `project_data.yaml`, write_section/read_document
tools, skip rules):

- **AWS Bedrock** (`AI_PROVIDER=bedrock`, `AI_MODEL_ID` required) —
  `app/infra/bedrock_runner.py`, with explicit prompt-cache checkpoints.
  AWS credentials come from the standard chain / instance role.
- **OpenAI** (`AI_PROVIDER=openai`, `AI_API_KEY` required) —
  `app/infra/openai_runner.py` via the Chat Completions API.
  `AI_MODEL_ID` defaults to `gpt-4o`; set `AI_ENDPOINT` to target Azure
  OpenAI or an OpenAI-compatible gateway. Prompt caching is automatic on
  OpenAI's side; cached-token counts are reported in the job usage summary
  either way. `AI_STEP_MODELS` per-step overrides apply to both providers.

The `http` provider remains a simple base64-JSON contract for a custom
endpoint. Switching provider is just an env change — no code or prompt edits.

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
  infra/s3_client.py       fetch agent-prompt step files from S3 (local fallback)
  infra/ai_client.py       AI model call → pricing sheet + tender (placeholder if unset)
  features/projects/
    routes.py              /projects/* routes (list, detail, upload, context, tender)
    core.py                project persistence, documents, context-doc compilation
    tender.py              background runner for the Estimate tender workflow
    templates/             project_detail, _documents, _doc_uploader, _tender_status, …
  templates/               base.html, _nav.html, login.html (shared)
  static/                  favicon
_agent_prompts/            workflow-step markdown (local fallback for S3)
```

## Storage — local disk or AWS S3

Project data is written through a repository (`app/features/projects/repo.py`)
with two backends, chosen automatically:

- **AWS S3** when you connect a bucket on the homepage (**Cloud storage** panel).
  Each project is its own folder in the bucket. Creating a project creates the
  folder; **Save context** overwrites the project record; opening a project
  reads it all back — markup/waste %, uploaded documents, roofing context and
  both T&C sets.
- **Local disk** (default) when no bucket is connected — data lives under
  `DATA_DIR`.

Connect S3 from the homepage: enter bucket, region, and either access keys or
leave them blank to use the instance role / default AWS credential chain.
**Test connection** verifies the bucket; **Disconnect** reverts to local. The
connection (incl. keys) is stored in `DATA_DIR/s3_config.json` on the app's own
volume.

Each project's folder is named after the project, so a project called
"5 Ebury Street" produces this key layout in the connected bucket:

```
5 Ebury Street/project.json                 # the record (metadata, params, answers, section text, document index)
5 Ebury Street/documents/<filename>         # every uploaded / generated document
```

(Names are sanitised for S3 keys; a duplicate name is suffixed, e.g. "5 Ebury Street (2)".)

Local layout under `DATA_DIR` (default `/home/data`, a Docker volume):

| Path | Contents |
| --- | --- |
| `project_<id>.json` | full record: metadata + params + documents + answers |
| `uploads/<id>/` | the raw uploaded files |
| `s3_config.json` | saved S3 connection (if configured) |

All local JSON writes are atomic (temp file + rename). Switching backends does
not migrate existing projects — data written to local isn't auto-copied to S3
and vice-versa.

> Tests use [`moto`](https://github.com/getmoto/moto) to mock S3
> (`pip install "moto[s3]"`); it isn't a runtime dependency.

## Updating the question set

The UI is fully driven by `app/data/question_map.json`. Regenerate it from
the Question Map workbook and restart the container — no code changes needed.

## Health check

`GET /healthz` returns `ok` for container health probes.
