# Agent prompts (workflow steps)

These markdown files are the ordered workflow steps the AI model executes to
turn the compiled project context + project documents into a pricing sheet and
tender document.

In production they are stored in AWS S3 (set `AGENT_PROMPTS_S3_BUCKET` /
`AGENT_PROMPTS_S3_PREFIX`). This local copy is the fallback used when S3 is not
configured, so the workflow runs out of the box. Files are processed in
filename order — keep the numeric prefixes.

This README is ignored by the loader (only `NN_*.md` step files are read by
filename order; `README.md` sorts before them but contains no step directive —
adjust the loader filter if you want it skipped).
