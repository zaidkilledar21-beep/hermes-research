---
name: synthesize-project
description: Consolidate several already-completed research runs into ONE cross-run intelligence brief — the big-picture rollup across a body of related research. Use when the user wants findings from multiple prior runs combined, compared, or reconciled.
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [research, synthesis, consolidate]
    category: research
    required_environment_variables:
      - RESEARCH_API_USER
      - RESEARCH_API_PASS
---
# Cross-Run Synthesizer

## When to use
When the user wants the BIG PICTURE across multiple prior research runs — "consolidate my peptide
research", "combine the 3PL and payments findings", "what does it all mean together", "compare
across my runs", "give me the rollup". This does NOT run new research — it consolidates runs that
already delivered. For a fresh question, use `run-research` instead.

## What it does
Assembles the findings from the chosen delivered runs and produces one consolidated intelligence
brief via a two-model chain (Claude Opus drafts → Codex adversarially critiques → Claude revises).
The brief covers: the overall picture, where findings reinforce across runs, contradictions across
runs, decision implications, and remaining gaps — cited by run_id, and ending with an "Adversarial
review (Codex)" section showing what the critic challenged. Subscription-backed, ~free per run.

## Procedure (use the terminal tool for the curl calls)
1. Figure out WHICH runs to consolidate:
   - If the user names run numbers, use those.
   - Otherwise list recent runs and pick the DELIVERED ones relevant to the user's topic:
     ```
     curl -s -u "$RESEARCH_API_USER:$RESEARCH_API_PASS" http://localhost:8080/api/runs
     ```
     Returns `{"runs":[{"run_id":N,"question":"...","status":"..."}]}`. Only consolidate runs whose
     status is `delivered` (gated/failed runs have no findings to include). Match by question topic.
2. Submit the synthesis (comma or space separated run_ids; optional title):
   ```
   curl -s -u "$RESEARCH_API_USER:$RESEARCH_API_PASS" -X POST \
     http://localhost:8080/api/synthesize \
     --data-urlencode "run_ids=14,15,18" \
     --data-urlencode "title=Peptide 3PL + payments consolidation"
   ```
   Returns `{"synthesis_id": N, "status": "synthesizing", "poll": "/api/synthesis/N"}`.
3. Poll every ~20s until `done` is true (this runs THREE model calls, so allow up to ~10 minutes):
   ```
   curl -s -u "$RESEARCH_API_USER:$RESEARCH_API_PASS" http://localhost:8080/api/synthesis/N
   ```
4. When `done`:
   - status `delivered` → present `report_md` to the user verbatim (markdown brief + Codex critique).
   - status `failed` → tell the user it failed and quote `report_md` (it holds the reason).
5. If still running after ~10 min, give the user the synthesis_id and say it's taking longer than usual.

## Prohibited
- Never invent cross-run conclusions the brief did not return. Relay only what `report_md` contains.
- Never drop the honesty hedges (community-signal = anecdotal) or the contradictions/gaps sections.
- Never consolidate gated/failed runs as if they had findings — only `delivered` runs.
