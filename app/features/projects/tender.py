"""Background runner for the "Estimate tender" workflow.

Steps (each updates the job so the status panel can show progress):
  1. Compile all project context (incl. profit/waste %) into a single
     document named "Project Context <name>" and attach it to the project's
     uploaded documents.
  2. Retrieve the workflow-step markdown files from AWS S3 (falling back to the
     bundled local _agent_prompts folder when S3 isn't configured).
  3. Pass those steps + the project documents to the AI model to generate the
     pricing sheet and tender document.
  4. Store both outputs on the job for download from the front end.

Runs in a daemon thread (single-worker, in-memory job store), mirroring the
job pattern used elsewhere in the stack.
"""

from __future__ import annotations

import logging
import threading
import time

from app import sessions
from app.infra import ai_client, s3_client
from . import core

LOGGER = logging.getLogger(__name__)


def start(job_id: str) -> None:
    threading.Thread(target=_run, args=(job_id,), daemon=True).start()


def _run(job_id: str) -> None:
    job = sessions.get_tender_job(job_id)
    if not job:
        return
    try:
        job.status = "running"

        rec = core.get_project(job.project_id)
        if not rec:
            raise RuntimeError("Project not found.")

        # Step 1 — compile + attach the context document.
        job.step = "Compiling project context document"
        rec, context_name = core.generate_estimate(job.project_id)
        job.context_filename = context_name
        context_bytes = core.read_document_bytes(job.project_id, context_name) or b""
        context_markdown = context_bytes.decode("utf-8", errors="replace")

        # Step 2 — fetch workflow-step prompts (S3 → local fallback).
        job.step = "Retrieving workflow steps"
        prompts = s3_client.fetch_agent_prompts()
        job.prompts_count = len(prompts)
        job.prompt_source = prompts[0]["source"] if prompts else "none"

        # Step 3 — call the AI model with prompts + project documents.
        job.step = "Generating pricing sheet and tender document"
        export = core.context_export(rec)
        documents = core.project_document_payloads(rec)
        result = ai_client.generate_tender(
            export=export,
            context_markdown=context_markdown,
            prompts=prompts,
            documents=documents,
        )

        # Step 4 — store outputs.
        job.ai_used = bool(result.get("ai_used"))
        job.notes = result.get("notes", "")
        pricing = result["pricing"]
        tender = result["tender"]
        job.pricing_bytes = pricing["bytes"]
        job.pricing_filename = pricing["filename"]
        job.pricing_media_type = pricing["media_type"]
        job.tender_bytes = tender["bytes"]
        job.tender_filename = tender["filename"]
        job.tender_media_type = tender["media_type"]

        job.step = "Done"
        job.status = "done"
    except Exception as exc:  # noqa: BLE001 — surface any failure to the UI
        LOGGER.exception("Tender job %s failed", job_id)
        job.status = "error"
        job.error = str(exc)
    finally:
        job.finished_at = time.time()
