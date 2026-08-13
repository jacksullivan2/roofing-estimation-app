"""The tender step workflow on OpenAI chat completions.

Mirrors app/infra/bedrock_runner.py — same steps (01–09 draft, 10–11 final),
same shared ``project_data.yaml``, same tools (write_section /
read_document), same skip rules and output extraction. Only the transport
differs: requests to the OpenAI Chat Completions API (or an OpenAI-compatible
endpoint via AI_ENDPOINT, e.g. Azure OpenAI) with function calling.

Deliberately a separate module rather than a branch inside bedrock_runner:
the Bedrock path's explicit cachePoint layout is battle-tested and stays
untouched. OpenAI needs none of that — its prompt caching is automatic on
repeated prefixes (>1024 tokens), which our stable-first request layout
(system = preamble + corpus, first user = step prompt + snapshot) already
exploits. Cached-token counts come back in usage.prompt_tokens_details and
are mapped onto the shared Usage accounting so job status shows cache
effectiveness for either provider.

All provider-neutral logic is imported from bedrock_runner — one source of
truth for prompt selection, persistence, skip rules and extraction.
"""

from __future__ import annotations

import json
import logging
import time

from app import settings
from app.infra import corpus as corpus_mod
from app.infra.bedrock_runner import (  # provider-neutral, reused as-is
    DRAFT_STEPS,
    FINAL_STEPS,
    PIPELINE_PREAMBLE,
    PROJECT_DATA_FILENAME,   # noqa: F401 — re-exported for callers
    TOOLS,
    Usage,
    WorkflowError,
    WorkflowResult,
    _execute_tool,
    _load_project_data,
    _model_for,
    _save_project_data,
    _should_skip,
    _yaml_snapshot,
    extract_draft_items,     # noqa: F401 — re-exported so callers can use
    final_output,            # noqa: F401 — either runner interchangeably
    index_prompts,
)

LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Bedrock toolSpec -> OpenAI function-calling definitions                      #
# --------------------------------------------------------------------------- #

def _openai_tools() -> list[dict]:
    out = []
    for t in TOOLS:
        spec = t["toolSpec"]
        out.append({
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec["description"],
                "parameters": spec["inputSchema"]["json"],
            },
        })
    return out


# --------------------------------------------------------------------------- #
# Transport                                                                    #
# --------------------------------------------------------------------------- #

def _endpoint() -> str:
    return (settings.AI_ENDPOINT or "").strip() or settings.OPENAI_DEFAULT_ENDPOINT


def _model(step_no: str) -> str:
    return _model_for(step_no) or settings.OPENAI_DEFAULT_MODEL


def _chat(model: str, messages: list[dict]) -> dict:
    """One chat-completions request. Returns the raw response JSON."""
    import requests

    resp = requests.post(
        _endpoint(),
        headers={
            "Authorization": f"Bearer {settings.AI_API_KEY}",
            "Content-Type": "application/json",
        },
        data=json.dumps({
            "model": model,
            "messages": messages,
            "tools": _openai_tools(),
            "max_completion_tokens": settings.AI_MAX_OUTPUT_TOKENS,
        }),
        timeout=settings.AI_TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        raise WorkflowError(
            f"OpenAI API returned {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def _normalise_usage(u: dict) -> dict:
    """Map OpenAI usage onto the Bedrock-keyed Usage accumulator. OpenAI's
    prompt_tokens INCLUDES cached tokens, so fresh input = prompt - cached."""
    prompt = u.get("prompt_tokens", 0)
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    return {
        "inputTokens": max(prompt - cached, 0),
        "cacheReadInputTokens": cached,
        "cacheWriteInputTokens": 0,   # OpenAI doesn't charge/report cache writes
        "outputTokens": u.get("completion_tokens", 0),
    }


# --------------------------------------------------------------------------- #
# One step = one function-calling conversation                                 #
# --------------------------------------------------------------------------- #

def run_step(step_no: str, step_prompt: str, corpus: str, pid: str,
             project_data: dict, usage: Usage) -> list[str]:
    """Run one workflow step. Returns the project_data sections it wrote."""
    model = _model(step_no)
    snapshot = _yaml_snapshot(project_data)

    # Stable-first layout so OpenAI's automatic prefix caching engages across
    # the run's steps (system stays byte-identical) and across a step's tool
    # turns (system + first user message stay byte-identical).
    messages: list[dict] = [
        {"role": "system",
         "content": PIPELINE_PREAMBLE + "\n\n## PROJECT DOCUMENT CORPUS\n\n" + corpus},
        {"role": "user",
         "content": ("## YOUR STEP PROMPT\n\n" + step_prompt
                     + "\n\n## CURRENT project_data.yaml SNAPSHOT\n\n" + snapshot
                     + "\n\nCarry out your step now.")},
    ]
    owned: list[str] = []

    for turn in range(settings.AI_MAX_TOOL_TURNS):
        resp = _chat(model, messages)
        u = _normalise_usage(resp.get("usage", {}))
        usage.add(u)
        LOGGER.info("step %s turn %d [%s]: in=%s cache_read=%s out=%s",
                    step_no, turn + 1, model, u["inputTokens"],
                    u["cacheReadInputTokens"], u["outputTokens"])

        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tool_calls = msg.get("tool_calls") or []

        # Echo the assistant message back verbatim (required before tool replies).
        messages.append({"role": "assistant",
                         "content": msg.get("content") or "",
                         **({"tool_calls": tool_calls} if tool_calls else {})})

        if choice.get("finish_reason") != "tool_calls" or not tool_calls:
            return owned  # step finished; its write_section calls are applied

        for tc in tool_calls:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError as exc:
                out = f"ERROR: arguments were not valid JSON — {exc}. Retry."
            else:
                out = _execute_tool(fn.get("name", ""), args, pid,
                                    project_data, owned)
            messages.append({"role": "tool",
                             "tool_call_id": tc.get("id", ""),
                             "content": out[:100_000]})

    raise WorkflowError(
        f"Step {step_no} exceeded AI_MAX_TOOL_TURNS "
        f"({settings.AI_MAX_TOOL_TURNS}) without finishing.")


# --------------------------------------------------------------------------- #
# The workflow (same orchestration as bedrock_runner.run_workflow)             #
# --------------------------------------------------------------------------- #

def run_workflow(pass_: str, export: dict, prompts: list[dict],
                 documents: list[dict], on_progress=None) -> WorkflowResult:
    """Run the draft (01–09) or final (10–11) pass on OpenAI."""

    def progress(text: str) -> None:
        if on_progress:
            try:
                on_progress(text)
            except Exception:  # noqa: BLE001 — progress must never kill a run
                pass

    pid = export["project"]["id"]
    by_step = index_prompts(prompts)
    steps = DRAFT_STEPS if pass_ == "draft" else FINAL_STEPS
    steps = [s for s in steps if s in by_step]
    if not steps:
        raise WorkflowError(
            f"No prompt files found for the {pass_} pass — check the "
            "_agent_prompts folder / S3 prefix.")

    project_data = {} if pass_ == "draft" else _load_project_data(pid)
    if pass_ == "final":
        if not project_data:
            steps = [s for s in DRAFT_STEPS if s in by_step] + steps
        project_data["estimator_review"] = {
            "items": export.get("draft_items", []),
            "note": "Captured item-by-item by the app's review loop.",
        }

    progress("Building document corpus")
    generated = {d["filename"] for d in export.get("documents", [])
                 if d.get("generated")}
    corpus = corpus_mod.get_or_build(pid, documents, generated)

    usage = Usage()
    result = WorkflowResult(project_data=project_data, usage=usage)

    for no in steps:
        prompt = by_step[no]
        reason = _should_skip(no, project_data)
        if reason:
            project_data.setdefault("skipped_steps", {})[no] = reason
            _save_project_data(pid, project_data)
            result.steps_skipped.append(no)
            LOGGER.info("Step %s skipped: %s", no, reason)
            continue
        progress(f"AI step {no} — {prompt['name']} (OpenAI)")
        started = time.time()
        owned = run_step(no, prompt["text"], corpus, pid, project_data, usage)
        project_data.setdefault("workflow_run", {})[no] = {
            "prompt_file": prompt["name"],
            "sections_written": owned,
            "duration_s": round(time.time() - started, 1),
        }
        _save_project_data(pid, project_data)
        result.steps_run.append(no)

    LOGGER.info("%s pass (openai) complete for %s — %s",
                pass_, pid, usage.summary())
    return result
