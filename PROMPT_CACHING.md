# Prompt caching in the tender workflow

The "Estimate tender" workflow now runs as a real AI pipeline on AWS Bedrock:
one model call per workflow step (draft pass = prompts 01–09, final pass =
10–11), all steps sharing `project_data.yaml`, with **prompt caching** built
into the shape of every request. This note explains how to switch it on, how
it works, and how to check it's actually saving money.

## Switching it on

Set these environment variables (e.g. in `docker-compose.yml` or `.env`):

```
AI_PROVIDER=bedrock
AI_MODEL_ID=<a Bedrock Claude model id or inference profile ARN>
AWS_REGION=<region with Bedrock access, e.g. eu-west-2>
# plus standard AWS credentials (AWS_ACCESS_KEY_ID / _SECRET / role)
```

Nothing else is required. With these unset the app behaves exactly as before
(placeholder outputs), and any AI failure mid-run degrades back to the
placeholder path with the error shown in the job notes.

Optional tuning:

| Variable | Default | Meaning |
|---|---|---|
| `AI_CACHE_ENABLED` | `true` | Strip cache markers entirely if `false`. The runner also auto-retries without markers if the model/region rejects `cachePoint`. |
| `AI_MAX_TOOL_TURNS` | `12` | Max tool round-trips per step before the run errors. |
| `AI_MAX_OUTPUT_TOKENS` | `8192` | Per-call output cap. |
| `AI_CORPUS_DOC_CHAR_CAP` | `60000` | Per-document char cap in the cached corpus (full text stays reachable via the `read_document` tool). |
| `AI_STEP_MODELS` | `{}` | JSON map of step → model id, e.g. `{"01": "...haiku...", "02": "...haiku..."}` to run mechanical steps on a cheaper model. Caches are **per-model**, so group consecutive steps on the same model. |

## How the caching works

Bedrock caches the **byte-identical prefix** of a request (checked in the
order tools → system → messages, TTL ~5 minutes, refreshed on every hit) and
charges cached reads at ~10% of the normal input price. Every request the
runner builds is layered stable-first:

1. **Tool definitions + pipeline preamble + document corpus** (system, cache
   marker 1) — identical for every step of a run. Written to cache once at
   step 01, then read ~30–40 times at 10% price by every later step and turn.
2. **Step prompt + `project_data.yaml` snapshot** (first user message, cache
   marker 2) — stable across all tool turns within one step.
3. **The growing tool conversation** — a third, "sliding" marker sits on the
   newest tool-result message so each turn re-reads the whole prior
   conversation from cache and only pays full price for what's new.

What keeps it working (`app/infra/corpus.py` enforces the first two):

- The corpus is **deterministic** — sorted filenames, fixed formatting, no
  timestamps — and persisted per-project (`_document_corpus.txt`) with a
  content hash, so unchanged documents produce byte-identical corpora even
  across runs. App-generated files (the "Project Context …" doc, which
  contains a timestamp) are excluded.
- Nothing volatile (job ids, times) appears before a cache marker.
- Steps run **back-to-back** — a stall longer than ~5 minutes between steps
  goes cold and re-pays the corpus cache write. The draft→final gap (the
  estimator's review) is always cold; that's fine, the final pass never
  re-reads the documents.

## Verifying it's working

Every model call logs a line like:

```
step 03 turn 2 [eu.anthropic...]: in=412 cache_read=58210 cache_write=0 out=1873
```

and each job stores totals in `job.usage`, summarised in the job notes, e.g.
"input 640,000 tokens (91% from cache: …)". The healthy pattern: the first
call of a run shows a large `cache_write`; everything after shows large
`cache_read` and small `in`. A large `in` mid-run means something before a
marker changed — usual suspects are an edited prompt file, a changed document
set (corpus rebuilt), or a >5-minute stall between steps.

## Files involved

- `app/infra/corpus.py` — deterministic document→text corpus with on-disk re-use.
- `app/infra/bedrock_runner.py` — the step-runner: cache layout, tool loop
  (`write_section` / `read_document`), skip logic, usage accounting.
- `app/infra/ai_client.py` — draft/final entry points; builds the pricing
  .xlsx from step 11's `pricing_rows` and the tender .md from
  `tender_markdown`; placeholder fallback on any failure.
- `app/features/projects/tender.py` — surfaces per-step progress and usage
  on the job status panel.
