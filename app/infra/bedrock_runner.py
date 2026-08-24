"""Sequential step-runner for the tender workflow on AWS Bedrock,
built around prompt caching.

Replaces the "one giant concatenated model call" design with what the agent
prompts actually describe: one model call per workflow step (01–09 for the
draft pass, 10–11 for the final pass), each step reading and writing the
shared ``project_data.yaml``.

Prompt-caching layout — the reason this file is shaped the way it is.
Bedrock caches the byte-identical PREFIX of a request (default TTL ~5 min,
refreshed on every hit), checked in the order tools → system → messages.
Every request this runner builds is therefore layered stable-first:

    toolConfig   tool definitions            identical for all steps & runs
    system       PIPELINE_PREAMBLE           identical for all steps & runs
                 document corpus             identical across a run's steps
                 [cachePoint 1]              ← written once, read ~30-40×
    messages[0]  step prompt + yaml snapshot stable within one step
                 [cachePoint 2]              ← read on every tool turn
    messages[n]  tool results, growing       [cachePoint 3] slides forward

So the expensive block (the corpus) is paid for once at step 01 and read at
10% price by everything after it. Rules that keep this true:

  * nothing volatile (timestamps, job ids) may appear before a cachePoint;
  * steps run back-to-back — a >5 min stall between steps goes cold;
  * the corpus comes from app.infra.corpus, which guarantees stable bytes.

Usage counters from every response are accumulated so the job status can
show fresh vs cached input tokens — the proof the caching is working.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

from app import settings
from app.infra import corpus as corpus_mod

LOGGER = logging.getLogger(__name__)

PROJECT_DATA_FILENAME = "project_data.yaml"

DRAFT_STEPS = ["01", "02", "03", "04", "05", "06", "07", "08", "09"]
FINAL_STEPS = ["10", "11"]

# Extraction steps that can be skipped when file categorisation (step 02)
# found no document of their category. Matching is by substring against the
# categories step 02 recorded; if the file_index shape is unrecognisable we
# run the step anyway — running an unnecessary step is cheap, skipping a
# necessary one is not.
SKIP_RULES: dict[str, tuple[str, ...]] = {
    "03": ("statement_of_works", "schedule_of_works", "sow"),
    "04": ("condition_report", "condition_survey"),
    "05": ("product_specification", "specification"),
    "06": ("manufacturer_pricing", "price_list", "pricing", "quote"),
    "07": ("labour_rates", "rate_card", "labour"),
}


class WorkflowError(RuntimeError):
    """Raised when a step cannot be completed."""


# --------------------------------------------------------------------------- #
# Usage accounting                                                            #
# --------------------------------------------------------------------------- #

@dataclass
class Usage:
    input_tokens: int = 0          # fresh, full-price input
    cache_read_tokens: int = 0     # cached input @ ~10% price
    cache_write_tokens: int = 0    # cache writes @ 125% price
    output_tokens: int = 0
    calls: int = 0

    def add(self, u: dict) -> None:
        self.calls += 1
        self.input_tokens += u.get("inputTokens", 0)
        self.output_tokens += u.get("outputTokens", 0)
        self.cache_read_tokens += u.get("cacheReadInputTokens", 0)
        self.cache_write_tokens += u.get("cacheWriteInputTokens", 0)

    def summary(self) -> str:
        total_in = self.input_tokens + self.cache_read_tokens + self.cache_write_tokens
        pct = (100 * self.cache_read_tokens / total_in) if total_in else 0
        return (f"{self.calls} model call(s); input {total_in:,} tokens "
                f"({pct:.0f}% from cache: {self.cache_read_tokens:,} read / "
                f"{self.cache_write_tokens:,} written / {self.input_tokens:,} fresh); "
                f"output {self.output_tokens:,} tokens")

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "output_tokens": self.output_tokens,
        }


@dataclass
class WorkflowResult:
    project_data: dict
    usage: Usage
    steps_run: list[str] = field(default_factory=list)
    steps_skipped: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Static request parts (MUST stay byte-stable — they sit before cache points) #
# --------------------------------------------------------------------------- #

PIPELINE_PREAMBLE = """\
You are one step of a multi-step roofing tender-estimation workflow. Each
step has its own prompt (supplied in the user message) and all steps share
one data file, project_data.yaml. You can see the current snapshot of that
file in the user message.

Environment notes (these complement, and never override, your step prompt):

* All project documents have been converted to text and appear below under
  PROJECT DOCUMENT CORPUS. Long documents may be truncated — call the
  read_document tool with the exact filename to get the full text.
* To record your step's output, call the write_section tool with the
  top-level key your step owns and the complete YAML content of that
  section. Writing a section replaces it entirely. Only write sections your
  step prompt says you own.
* When your step prompt says to produce final deliverables (final
  generation step), write a section named final_output containing:
    pricing_rows: a list of row objects with keys
      group, item, detail, qty, unit, unit_rate, material, labour,
      line_total, reasoning, qualifications
    tender_markdown: the complete client-facing tender document as Markdown.
* Finish by making sure every section you own has been written via
  write_section, then reply with a one-paragraph plain-text summary of what
  you did. Do not paste YAML into your reply text.
"""

TOOLS = [
    {
        "toolSpec": {
            "name": "write_section",
            "description": ("Write or replace one top-level section of the shared "
                            "project_data.yaml. Use only for sections your step owns. "
                            "The content must be valid YAML for that section's value."),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "section": {"type": "string",
                                "description": "Top-level key to write, e.g. file_index"},
                    "content_yaml": {"type": "string",
                                     "description": "YAML for the section's value"},
                },
                "required": ["section", "content_yaml"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "read_document",
            "description": ("Return the full extracted text of one uploaded project "
                            "document (use when the corpus copy is truncated). "
                            "Pass the exact filename shown in the corpus header."),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {"filename": {"type": "string"}},
                "required": ["filename"],
            }},
        }
    },
]

_CACHE_POINT = {"cachePoint": {"type": "default"}}


# --------------------------------------------------------------------------- #
# Prompt selection                                                            #
# --------------------------------------------------------------------------- #

def index_prompts(prompts: list[dict]) -> dict[str, dict]:
    """Group prompt files by their two-digit step prefix. Where both a full
    prompt ("01_project_intake_prompt.md") and a legacy stub ("01_intake.md")
    exist, prefer the file with "_prompt" in its name, else the longest."""
    by_step: dict[str, dict] = {}
    for p in prompts:
        m = re.match(r"^(\d{2})_", p["name"])
        if not m:
            continue
        no = m.group(1)
        cur = by_step.get(no)
        if cur is None:
            by_step[no] = p
            continue
        p_is_full = "_prompt" in p["name"].lower()
        c_is_full = "_prompt" in cur["name"].lower()
        if (p_is_full, len(p["text"])) > (c_is_full, len(cur["text"])):
            by_step[no] = p
    return by_step


# --------------------------------------------------------------------------- #
# project_data.yaml persistence                                               #
# --------------------------------------------------------------------------- #

def _seed_project_data(pass_: str, export: dict, project_data: dict) -> None:
    """Facts the app already knows are written by the app, never the model.

    Without this, step 01 has been observed inventing a project identity out
    of its prompt's worked example (id p-cranley-sw7, client "Rosewood Ltd")
    and every later step inherited it. Models also have no clock, so real
    run timestamps are stamped here for the steps to reference.
    """
    from datetime import datetime, timezone

    proj = export.get("project") or {}
    project_data["project"] = {
        "id": proj.get("id"),
        "name": proj.get("name"),
        "client": proj.get("client"),
        "reference": proj.get("reference"),
        "markup_pct": proj.get("markup_pct"),
        "waste_pct": proj.get("waste_pct"),
        "seeded_by": "app",
        "note": ("Written by the application from the project record — "
                 "authoritative. Do not rewrite; extra extracted metadata "
                 "goes under 'project_extra'."),
    }
    project_data.setdefault("run_info", {})[pass_] = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": ("Real UTC timestamp stamped by the application. Use it for "
                 "any *_at / generated_at fields instead of inventing dates."),
    }


def _load_project_data(pid: str) -> dict:
    import yaml
    from app.features.projects import repo as _repo

    raw = _repo.get_repo().read_document(pid, PROJECT_DATA_FILENAME)
    if raw is None:
        return {}
    try:
        data = yaml.safe_load(raw.decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError as exc:
        LOGGER.warning("Stored project_data.yaml unreadable (%s); starting fresh.", exc)
        return {}


def _save_project_data(pid: str, data: dict) -> None:
    import yaml
    from app.features.projects import repo as _repo

    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True,
                          default_flow_style=False)
    _repo.get_repo().write_document(pid, PROJECT_DATA_FILENAME, body.encode("utf-8"))


def _yaml_snapshot(data: dict) -> str:
    import yaml
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True,
                          default_flow_style=False)


# --------------------------------------------------------------------------- #
# Skip logic                                                                  #
# --------------------------------------------------------------------------- #

def _categories_in(node) -> set[str]:
    """Collect category-ish string values anywhere inside the file_index."""
    found: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str) and "categor" in str(k).lower():
                found.add(v.strip().lower().replace(" ", "_"))
            else:
                found |= _categories_in(v)
    elif isinstance(node, list):
        for v in node:
            found |= _categories_in(v)
    return found


def _should_skip(step_no: str, project_data: dict) -> str | None:
    """Return a skip reason, or None to run the step."""
    tokens = SKIP_RULES.get(step_no)
    if not tokens:
        return None
    file_index = project_data.get("file_index")
    if not file_index:
        return None  # categorisation output missing/unrecognised — run the step
    cats = _categories_in(file_index)
    if not cats:
        return None
    joined = " ".join(cats)
    if any(t in joined for t in tokens):
        return None
    return (f"no document of category {tokens[0]} in file_index "
            f"(categories present: {', '.join(sorted(cats))})")


# --------------------------------------------------------------------------- #
# One step = one cached Converse conversation                                 #
# --------------------------------------------------------------------------- #

def _model_for(step_no: str) -> str:
    return settings.AI_STEP_MODELS.get(step_no) or settings.AI_MODEL_ID


def _tool_config() -> dict:
    # No cachePoint on tools: the definitions are small (< the model's minimum
    # cacheable size) and system cachePoint 1 covers the tools+system prefix.
    return {"tools": list(TOOLS)}


def _system_blocks(corpus: str, cache: bool) -> list[dict]:
    blocks = [
        {"text": PIPELINE_PREAMBLE},
        {"text": "## PROJECT DOCUMENT CORPUS\n\n" + corpus},
    ]
    if cache:
        blocks.append(dict(_CACHE_POINT))  # cachePoint 1 — corpus boundary
    return blocks


def _first_message(step_prompt: str, snapshot: str, cache: bool) -> dict:
    content = [
        {"text": "## YOUR STEP PROMPT\n\n" + step_prompt},
        {"text": "## CURRENT project_data.yaml SNAPSHOT\n\n" + snapshot},
        {"text": "Carry out your step now."},
    ]
    if cache:
        content.append(dict(_CACHE_POINT))  # cachePoint 2 — step boundary
    return {"role": "user", "content": content}


def _strip_cache_points(messages: list[dict]) -> None:
    for m in messages:
        m["content"] = [b for b in m.get("content", []) if "cachePoint" not in b]


def _error_window(raw: str, pos: int, width: int = 80) -> str:
    """The text surrounding an error position, one line, with a marker at the
    exact spot — so the model repairs by sight instead of from memory."""
    before = " ".join(raw[max(0, pos - width):pos].split())
    after = " ".join(raw[pos:pos + width].split())
    return f"...{before} >>>ERROR IS HERE>>> {after}..."


def _parse_section_content(raw: str) -> tuple:
    """Parse write_section content. JSON-looking content goes through the
    strict JSON parser FIRST (precise errors, none of YAML's flow-parsing
    quirks); everything else through YAML. Returns (value, None) on success
    or (None, error_message) on failure."""
    import yaml

    if raw.lstrip().startswith(("{", "[")):
        try:
            return json.loads(raw), None
        except json.JSONDecodeError as exc:
            return None, (
                f"ERROR: content looks like JSON but is not valid JSON — "
                f"{exc.msg} at position {exc.pos}. The broken spot is: "
                f"{_error_window(raw, exc.pos)} Fix exactly that spot and "
                "resend the COMPLETE content.")
    try:
        return yaml.safe_load(raw), None
    except yaml.YAMLError as exc:
        pos = getattr(getattr(exc, "problem_mark", None), "index", None)
        window = f" The broken spot is: {_error_window(raw, pos)}" \
            if pos is not None else ""
        hint = ("Your content ended mid-document (unclosed brackets or "
                "quotes) — you stopped before finishing it. Resend the "
                "COMPLETE content; if it is long, shorten field values but "
                "never stop early."
                if "stream end" in str(exc) else
                "Do NOT resend the same text. Resend the ENTIRE section "
                "content as strict JSON (one object, double-quoted keys, no "
                "comments) — JSON is accepted here and is easier to get "
                "right than indented YAML.")
        return None, (f"ERROR: content_yaml is not valid YAML — {exc}."
                      f"{window} {hint}")


def _execute_tool(name: str, tool_input: dict, pid: str,
                  project_data: dict, owned: list[str]) -> str:
    if name == "read_document":
        text = corpus_mod.full_document_text(pid, tool_input.get("filename", ""))
        cap = settings.AI_READ_DOC_CHAR_CAP
        if cap and len(text) > cap:
            text = (text[:cap]
                    + f"\n[…truncated at {cap:,} characters to protect the "
                      "rate-limit budget. Work from what is shown; the "
                      "beginning of a document carries its structure.]")
        return text
    if name == "write_section":
        section = (tool_input.get("section") or "").strip()
        if not section:
            return "ERROR: section name is required."
        value, parse_error = _parse_section_content(
            tool_input.get("content_yaml") or "")
        if parse_error:
            return parse_error
        if (section == "project"
                and isinstance(project_data.get("project"), dict)
                and project_data["project"].get("seeded_by") == "app"):
            return ("ERROR: not applied — section 'project' was seeded by the "
                    "application from the real project record and is "
                    "authoritative. Do not rewrite it. Put any additional "
                    "project metadata you extracted under 'project_extra'.")
        if section in project_data:
            old_size = len(str(project_data[section]))
            new_size = len(str(value))
            if old_size >= 1000 and new_size < old_size // 4:
                return (f"ERROR: not applied — this would replace section "
                        f"'{section}' ({old_size} chars) with only {new_size} "
                        "chars. write_section REPLACES the entire section; a "
                        "small write here would destroy what is already "
                        "stored. If you are adding metadata or a supplement, "
                        "write it under its own key (e.g. 'extraction_meta'); "
                        "if you truly mean to rewrite this section, resend the "
                        "COMPLETE content including everything already there.")
        project_data[section] = value
        if section not in owned:
            owned.append(section)
        _save_project_data(pid, project_data)
        return f"OK: section '{section}' written ({len(str(value))} chars)."
    return f"ERROR: unknown tool {name}."


def run_step(client, step_no: str, step_prompt: str, corpus: str,
             pid: str, project_data: dict, usage: Usage) -> list[str]:
    """Run one workflow step as a tool-use conversation. Returns the list of
    project_data sections the step wrote."""
    cache = settings.AI_CACHE_ENABLED
    model_id = _model_for(step_no)
    if not model_id:
        raise WorkflowError("AI_MODEL_ID is not set.")

    snapshot = _yaml_snapshot(project_data)
    messages = [_first_message(step_prompt, snapshot, cache)]
    owned: list[str] = []
    truncations = 0

    for turn in range(settings.AI_MAX_TOOL_TURNS):
        kwargs = {
            "modelId": model_id,
            "system": _system_blocks(corpus, cache),
            "messages": messages,
            "toolConfig": _tool_config(),
            "inferenceConfig": {"maxTokens": settings.AI_MAX_OUTPUT_TOKENS},
        }
        try:
            resp = client.converse(**kwargs)
        except Exception as exc:  # noqa: BLE001
            # Some models/regions reject cachePoint blocks — retry uncached
            # rather than failing the whole run.
            if cache and "cachePoint" in str(exc):
                LOGGER.warning("cachePoint rejected by %s — retrying without "
                               "caching. (%s)", model_id, exc)
                cache = False
                _strip_cache_points(messages)
                kwargs["system"] = _system_blocks(corpus, cache)
                resp = client.converse(**kwargs)
            else:
                raise

        u = resp.get("usage", {})
        usage.add(u)
        LOGGER.info(
            "step %s turn %d [%s]: in=%s cache_read=%s cache_write=%s out=%s",
            step_no, turn + 1, model_id, u.get("inputTokens"),
            u.get("cacheReadInputTokens"), u.get("cacheWriteInputTokens"),
            u.get("outputTokens"))

        msg = resp["output"]["message"]
        messages.append(msg)

        stop = resp.get("stopReason")
        if stop == "max_tokens":
            # The response was cut off by the output-token ceiling, so any
            # in-flight tool call was discarded. Without this branch the step
            # would "finish" silently with nothing written.
            if truncations >= 2:
                raise WorkflowError(
                    f"Step {step_no}: output truncated by the "
                    f"{settings.AI_MAX_OUTPUT_TOKENS}-token output limit "
                    "3 times in a row — raise AI_MAX_OUTPUT_TOKENS in .env "
                    "or make this step's output smaller.")
            truncations += 1
            LOGGER.warning(
                "step %s turn %d: output truncated at %s tokens — telling "
                "the model to resend more concisely (%d/2).",
                step_no, turn + 1, settings.AI_MAX_OUTPUT_TOKENS, truncations)
            messages.append({"role": "user", "content": [{"text": (
                "Your previous response was CUT OFF by the maximum output "
                "length before your tool call completed, so NOTHING was "
                "saved. Re-issue the write_section call now with the same "
                "structure but more concise content: shorten description/"
                "detail field values, drop repetition, keep every item and "
                "every required key. Do not apologise or explain — just "
                "make the tool call.")}]})
            continue

        if stop != "tool_use":
            return owned  # step finished (its write_section calls are applied)

        results = []
        for block in msg.get("content", []):
            tu = block.get("toolUse")
            if not tu:
                continue
            out = _execute_tool(tu["name"], tu.get("input") or {},
                                pid, project_data, owned)
            results.append({"toolResult": {
                "toolUseId": tu["toolUseId"],
                "content": [{"text": out[:100_000]}],
            }})

        # Slide cachePoint 3 forward: strip the marker from earlier
        # tool-result messages so only the newest one carries it (Bedrock
        # allows 4 markers max; messages[0] keeps its step-boundary marker,
        # and each new turn re-reads the whole prior conversation from cache).
        if cache:
            for m in messages[1:]:
                if m.get("role") == "user":
                    m["content"] = [b for b in m["content"]
                                    if "cachePoint" not in b]
            results.append(dict(_CACHE_POINT))
        messages.append({"role": "user", "content": results})

    raise WorkflowError(
        f"Step {step_no} exceeded AI_MAX_TOOL_TURNS "
        f"({settings.AI_MAX_TOOL_TURNS}) without finishing.")


# --------------------------------------------------------------------------- #
# The workflow                                                                #
# --------------------------------------------------------------------------- #

def run_workflow(pass_: str, export: dict, prompts: list[dict],
                 documents: list[dict], on_progress=None) -> WorkflowResult:
    """Run the draft (steps 01–09) or final (10–11) pass for a project.

    `export` is core.context_export(rec); `documents` the [{filename, bytes}]
    payloads (incl. any shared labour-rates doc). Steps run back-to-back on
    purpose: Bedrock's cache lives ~5 minutes, so a stall between steps would
    re-pay the corpus cache write.
    """
    import boto3

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
    _seed_project_data(pass_, export, project_data)
    if pass_ == "final":
        if not project_data:
            # Final requested with no draft on file — run the full pipeline.
            steps = [s for s in DRAFT_STEPS if s in by_step] + steps
        # Hand the estimator's per-item review to the merge step (runner-owned
        # section; the estimator's words are recorded verbatim).
        project_data["estimator_review"] = {
            "items": export.get("draft_items", []),
            "note": "Captured item-by-item by the app's review loop.",
        }

    progress("Building document corpus")
    generated = {d["filename"] for d in export.get("documents", [])
                 if d.get("generated")}
    corpus = corpus_mod.get_or_build(pid, documents, generated)

    # Reasoning models can legitimately take >60s per turn; botocore's
    # default 60s read timeout aborts mid-generation and silently retries,
    # which is how a run stalls for minutes then dies with TimeoutError.
    from botocore.config import Config as _BotoConfig
    kwargs = {"config": _BotoConfig(
        connect_timeout=10,
        read_timeout=settings.AI_TIMEOUT_SECONDS,
        retries={"max_attempts": 4, "mode": "adaptive"},
    )}
    if settings.AWS_REGION:
        kwargs["region_name"] = settings.AWS_REGION
    client = boto3.client("bedrock-runtime", **kwargs)

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
        progress(f"AI step {no} — {prompt['name']}")
        started = time.time()
        owned = run_step(client, no, prompt["text"], corpus, pid,
                         project_data, usage)
        if not owned:
            raise WorkflowError(
                f"Step {no} ({prompt['name']}) completed without writing any "
                "section to project_data.yaml — failing the run instead of "
                "continuing with that step's output silently missing.")
        project_data.setdefault("workflow_run", {})[no] = {
            "prompt_file": prompt["name"],
            "sections_written": owned,
            "duration_s": round(time.time() - started, 1),
        }
        _save_project_data(pid, project_data)
        result.steps_run.append(no)

    LOGGER.info("%s pass complete for %s — %s", pass_, pid, usage.summary())
    return result


# --------------------------------------------------------------------------- #
# Pulling app-shaped outputs back out of project_data                         #
# --------------------------------------------------------------------------- #

_ITEM_LIST_KEYS = ("items", "work_items", "draft_items", "line_items",
                   "pricing_items", "rows")


def _find_item_list(node) -> list[dict] | None:
    """Depth-first hunt for a list of dicts under an item-ish key."""
    if isinstance(node, dict):
        for key in _ITEM_LIST_KEYS:
            v = node.get(key)
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                return v
        for v in node.values():
            found = _find_item_list(v)
            if found:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find_item_list(v)
            if found:
                return found
    return None


def _first(d: dict, *keys, default="") -> str:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return str(v)
    return default


def extract_draft_items(project_data: dict, step_sections: list[str] | None = None) -> list[dict]:
    """Map step 09's output into the {group,item,detail,qty,unit} items the
    review UI expects. Tolerant of schema drift in the prompt outputs."""
    source = None
    for section in (step_sections or []):
        source = _find_item_list(project_data.get(section))
        if source:
            break
    if not source:
        source = _find_item_list(project_data)
    if not source:
        return []

    items: list[dict] = []
    for it in source:
        detail = _first(it, "detail", "description", "scope")
        reasoning = _first(it, "reasoning", "reasoning_source", "source")
        if reasoning:
            detail = f"{detail}\n\nReasoning: {reasoning}" if detail else reasoning
        items.append({
            "group": _first(it, "group", "section", "area", "element_group"),
            "item": _first(it, "item", "name", "title", "work_item"),
            "detail": detail,
            "qty": _first(it, "qty", "quantity"),
            "unit": _first(it, "unit", "uom"),
        })
    return items


def final_output(project_data: dict) -> dict | None:
    """The final_output section written by the last step, if present."""
    out = project_data.get("final_output")
    return out if isinstance(out, dict) else None
