# Phase 1 — synthesis failure semantics + granular release gate

## Objective
A research pipeline currently loses entire valid reports to two defects: (1) a JSON parse failure is
reported as "no findings produced", indistinguishable from an honest negative result; (2) ONE
malformed finding hard-blocks an entire otherwise-valid ~19-finding report. Fix both, and stop
silently laundering fabricated evidence IDs so the integrity gate can actually see them.

## Files to MODIFY (only these two)
- `pipeline/synthesize.py`
- `pipeline/release_gate.py`

## Files to CREATE
- `tests/test_synthesis_parse.py` — unit tests, pure functions only, NO database, NO network.

## Files you MUST NOT TOUCH
`pipeline/run.py`, `pipeline/reviewers.py`, `pipeline/extract.py`, `pipeline/report.py`,
`pipeline/cross_synthesize.py`, `pipeline/queries.py`, `pipeline/reach_bridge.py`,
`collectors/*`, `reach/*`, `reviewer/*`, `web/*`, `db/*`, `deploy/*`, `tasks/*`, `hermes-skills/*`.
Do NOT create migrations — the DB columns below already exist. Do NOT run deployments or ssh.

## Database columns (ALREADY APPLIED — code against these, do not create them)
```
research_runs.synthesis_state   TEXT    -- ok | valid_empty | parse_failed | truncated | schema_invalid | transport_failed
research_runs.synthesis_meta    JSONB   -- {finish_reason, model, content_len, reasoning_len, validation_errors[], attempts}
research_runs.synthesis_raw     TEXT    -- raw model response (truncate to 20000 chars)
findings.disposition            TEXT NOT NULL DEFAULT 'accepted'
findings.disposition_detail     TEXT
```

## Required behaviour — `pipeline/synthesize.py`

1. **Stop laundering fabricated evidence IDs.** Currently line ~171 filters model-returned
   `evidence_ids` down to valid ones before insert, which makes the gate's "cites nonexistent
   evidence" check dead code AND converts a fabrication into an unexplained empty-citation finding.
   **Store EXACTLY what the model returned** (ints only; drop non-integer junk). The gate validates.

2. **Failure-state taxonomy.** `synthesize()` must classify every outcome and persist
   `synthesis_state` + `synthesis_meta` + `synthesis_raw` on `research_runs`:
   - `ok` — findings parsed (>=1)
   - `valid_empty` — model returned well-formed JSON with an empty `findings` array (a genuine
     "nothing found"); this is the ONLY case allowed to mean "no findings"
   - `parse_failed` — response present but no valid findings array extractable
   - `truncated` — `finish_reason` indicates length/truncation
   - `schema_invalid` — parsed JSON but wrong shape (e.g. `findings` not a list)
   - `transport_failed` — HTTP/network/exception before a response
   Never let a parse failure be reported as "no findings".

3. **One bounded repair retry.** If the first response is `parse_failed`/`schema_invalid`, make at
   most ONE additional model call appending a short repair instruction (restate the exact required
   JSON shape, tell it to return ONLY that JSON). If the retry also fails, persist the failure state
   and return 0. Record `attempts` in `synthesis_meta`. Never loop more than twice total.

4. **Keep existing behaviour otherwise**: budget check, `log_agent_run` cost logging, the two-phase
   insert with `contradicts_idx` → `contradicts` mapping, and the `_norm_label` normalisation.
   IMPORTANT: when a label is invalid, still normalise it to `unknown` for the `label` column BUT
   set `disposition='quarantined_invalid_label'` and record the original label in
   `disposition_detail` — do not hide the violation.

5. **Refactor the parser for testability.** Extract pure functions that take a string / dict and
   return results with NO database or network access, so tests can exercise them directly. Suggested:
   `classify_response(msg: dict, finish_reason: str|None) -> tuple[list, str, dict]` returning
   `(findings, state, meta)`. Keep the existing fence-stripping and outermost-object fallbacks and
   ADD handling for: a bare top-level JSON array of findings; `{"result": {"findings": [...]}}`;
   and JSON preceded/followed by prose.

## Required behaviour — `pipeline/release_gate.py`

Replace all-or-nothing blocking with per-finding dispositions.

`check(run_id)` must return a structured result (keep a backwards-compatible list of blocking
problem strings available, since `pipeline/run.py` currently does `problems = release_gate.check(...)`
and treats a truthy value as "blocked" — **do not break that call signature**; prefer returning the
same `list[str]` of SYSTEMIC problems only, and write per-finding dispositions to the DB as a side
effect).

**Finding-local → set `disposition`, do NOT block the run:**
- assertive label (`observed`/`inferred`/`community_signal`) with zero evidence ids →
  `quarantined_no_evidence`
- cites evidence ids not present in this run → `quarantined_fabricated_ids`, and put the offending
  ids in `disposition_detail`
- invalid label → `quarantined_invalid_label`
- a reviewer row with `severity='reject'` for that finding → `rejected_by_reviewer`, detail = reviewer + detail
- everything else → `accepted`

**SYSTEMIC → block (return non-empty list):**
- `synthesis_state` is not `ok` and not `valid_empty`
- more than 50% of findings quarantined/rejected (widespread malformation or fabrication)
- zero accepted findings remain
Budget overrun must NOT block delivery — record it as a warning problem string prefixed `WARN:` that
the caller can surface but which does not count as blocking. (Spending is already stopped upstream.)

`valid_empty` with zero findings is NOT a systemic failure — it should deliver an
"insufficient evidence" outcome, not look like a crash.

## Tests — `tests/test_synthesis_parse.py`
Plain `unittest` or `pytest`, pure functions only, no DB/network/mocks of psycopg. Cover at minimum:
- clean `{"findings":[...]}` → `ok`
- ```json fenced``` payload → `ok`
- prose before and after the JSON object → `ok`
- bare top-level array → `ok`
- `{"findings":[]}` → `valid_empty`
- non-JSON prose only → `parse_failed`
- `finish_reason="length"` with unparseable tail → `truncated`
- `{"findings":{}}` (wrong type) → `schema_invalid`
- fabricated/non-integer evidence ids are preserved as ints where possible, junk dropped

## Constraints
- Python 3, stdlib + existing deps only (`psycopg`, `requests`). Do not add dependencies.
- Match the existing code style: module docstring, comments explain WHY not WHAT, no emoji.
- Preserve all existing module-level constants and env-var names.
- Do not reformat or restructure unrelated code.

## Verification you must run before reporting done
- `python -m py_compile pipeline/synthesize.py pipeline/release_gate.py`
- `python -m pytest tests/test_synthesis_parse.py -q` (or `python -m unittest`) — all pass
Report exactly which tests pass and any behaviour you were unsure about.
