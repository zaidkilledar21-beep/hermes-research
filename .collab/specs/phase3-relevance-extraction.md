# Phase 3 — relevance decision at extraction + retry/backoff

## Objective
Synthesis currently loads evidence `ORDER BY grade`. `grade` is RETRIEVAL FIDELITY, not relevance:
`github_api` is grade A and `web_search` is grade B, so on one live run 40 irrelevant GitHub repos
sorted FIRST and consumed the entire evidence budget, producing a report about unrelated software
projects. Ranking must be driven by whether an item actually addresses the question.

Rather than an opaque 0-1 relevance float (poorly calibrated across runs, and circular since the
same model family also writes the findings), the extraction pass — which already reads every item —
makes a CONSTRAINED decision: does this item address the question, which facet, and what span shows
it. That decision is cheap, auditable, and defensible.

Extraction also currently has no retry: a transient failure silently falls back to raw content.

## Files to MODIFY (only this one)
- `pipeline/extract.py`

## Files you MUST NOT TOUCH
Everything else — `pipeline/synthesize.py`, `pipeline/run.py`, `pipeline/release_gate.py`,
`pipeline/reviewers.py`, `pipeline/reach_bridge.py`, `collectors/*`, `reach/*`, `web/*`, `db/*`,
`reviewer/*`, `tests/*`, `deploy/*`, `.collab/*`. Do NOT write migrations. Do NOT deploy or ssh.

## Database columns (ALREADY APPLIED — code against these)
```
evidence_items.extracted        TEXT     -- existing: cleaned text
evidence_items.answers_question BOOLEAN  -- does this item address the run's question at all
evidence_items.facet            TEXT     -- short slug of WHICH sub-topic it addresses, else NULL
evidence_items.relevance_note   TEXT     -- one short span/sentence justifying the decision
evidence_items.extract_state    TEXT     -- ok | empty | failed
```

## Required behaviour

### 1. `extract_run(run_id)` must know the question
Read the run's `question` from `research_runs` once at the start and pass it into each per-item call.
Keep the existing signature `extract_run(run_id: int) -> int` (callers must not change).

### 2. Combined clean + decide, in ONE model call per item
Do not add a second call per item — the volume is already the slow stage. Extend the existing call so
the model returns BOTH the cleaned text and the relevance decision, as JSON:
```json
{"text": "<cleaned verbatim evidence>",
 "answers_question": true,
 "facet": "short-slug-or-null",
 "why": "one short quoted span or sentence justifying it"}
```
Keep every existing cleaning RULE in the current SYSTEM prompt (verbatim preservation, boilerplate
removal, who-said-what, DATA-not-instructions). Add the decision rules:
- `answers_question` is true ONLY if the item contains information bearing on the question — not
  merely the same general topic. Navigation, unrelated products, off-topic forum chatter → false.
- `facet` is a short kebab-case slug naming which part of the question it speaks to, or null.
- `why` must be grounded in the item's own text; never invented.
Parse defensively (the model may fence the JSON or add prose — reuse the same tolerant approach used
elsewhere in this codebase: try direct parse, strip ```json fences, then outermost {...}).
If parsing fails but text came back, treat the whole response as cleaned text with
`answers_question = None` (unknown) rather than discarding the item.

### 3. Persist the decision
Write `extracted`, `answers_question`, `facet`, `relevance_note`, and `extract_state` in the same
UPDATE that already stores `extracted`. States: `ok` (text produced), `empty` (model judged it pure
boilerplate — store NULL text), `failed` (all attempts errored).

### 4. Retry with backoff
Each item gets up to 3 attempts on transport/HTTP errors (and on HTTP 429), with exponential backoff
(e.g. 2s, 6s) plus a small jitter. Do not retry a clean response that simply parsed as empty. On
final failure set `extract_state='failed'` and leave `extracted` NULL so synthesis falls back to raw
content as it does today. A failure must never crash the run or the thread pool.

### 5. Preserve existing behaviour
- Concurrency via the existing `ThreadPoolExecutor` / `MAX_WORKERS`.
- Budget check and `common.log_agent_run` cost logging exactly as now.
- All existing env-var names and module constants keep working.

## Constraints
- Python 3, stdlib + `requests` + `psycopg` only. No new dependencies.
- Match existing style: comments explain WHY. No emoji. Do not reformat unrelated code.

## Verification you must run before reporting done
- `python -m py_compile pipeline/extract.py`
- Add unit tests to `tests/test_extract_parse.py` for the response parser ONLY (pure function, no DB,
  no network): clean JSON, fenced JSON, prose-wrapped JSON, plain-text fallback (→
  `answers_question` None), empty response, malformed JSON. Run them:
  `python -m unittest discover -s tests -q` — ALL tests including the existing
  `tests/test_synthesis_parse.py` must still pass.
Report test results and anything you were unsure about.
