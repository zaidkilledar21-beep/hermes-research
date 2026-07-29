# Spec: atomic run admission gate

## Objective

`pipeline.run --run N` is spawned as a bare process from three independent paths (the web app twice,
and an LLM agent's shell directly). Nothing limits how many exist. Today 15 accumulated, four of them
byte-identical re-asks of a question that had already delivered 18 findings.

Build a single atomic admission gate that a run must pass before doing any work. It checks three
conditions — concurrency, duplicate question, daily budget — in ONE SQL statement.

## Critical design constraints (do not deviate)

1. **NO advisory locks.** The database is Neon's **pooled** endpoint
   (`ep-...-pooler.c-2.us-west-2.aws.neon.tech`) = PgBouncer in transaction mode. Session-level
   advisory locks are not pinned to a backend across transactions. They appear to work in a single
   session and fail silently under real concurrency. Same trap as `docs/lessons.md` #26.
2. **NO read-then-decide.** `SELECT count(...)` followed by a Python `if` is a TOCTOU race: under the
   exact concurrency this gate exists to control, every worker reads "under limit" and every worker
   proceeds. The check and the reservation MUST be the same statement.
3. **Fail CLOSED.** This codebase is fail-soft everywhere, deliberately. Admission is the exception:
   if the database is unreachable, `admit()` returns `(False, "admission_unavailable")`. A guard that
   evaporates when its backing store blips is not a guard, and an ungoverned run is the precise
   failure being prevented. State this in the module docstring so the divergence is intentional and
   documented, not an oversight.

## Files to CREATE

### 1. `db/migration_015_run_admission.sql`

```sql
ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS question_hash text;

UPDATE research_runs
   SET question_hash = md5(lower(btrim(regexp_replace(question, '\s+', ' ', 'g'))))
 WHERE question_hash IS NULL;

CREATE INDEX IF NOT EXISTS idx_research_runs_qhash
    ON research_runs (question_hash, status);

CREATE TABLE IF NOT EXISTS run_admissions (
    run_id        integer PRIMARY KEY,
    question_hash text        NOT NULL,
    started_at    timestamptz NOT NULL DEFAULT now(),
    heartbeat_at  timestamptz NOT NULL DEFAULT now(),
    status        text        NOT NULL DEFAULT 'active'
);

CREATE INDEX IF NOT EXISTS idx_run_admissions_live
    ON run_admissions (status, heartbeat_at);
```

Follow the existing migration files' style (`db/migration_0*.sql`) — idempotent, commented with WHY.

### 2. `pipeline/admission.py`

Exact public interface:

```python
def question_hash(question: str) -> str: ...
def admit(run_id: int, question: str, *, force: bool = False) -> tuple[bool, str]: ...
def heartbeat(run_id: int) -> None: ...
def release(run_id: int, status: str = "finished") -> None: ...
```

**`question_hash`** MUST produce byte-identical output to the SQL expression in the migration.
Order: collapse whitespace runs to one space → strip → lowercase → md5 hex.
```python
hashlib.md5(re.sub(r"\s+", " ", question).strip().lower().encode("utf-8")).hexdigest()
```

**`admit`** — one statement, zero rows returned means refused:

```sql
INSERT INTO run_admissions (run_id, question_hash)
SELECT %(run_id)s, %(qhash)s
WHERE (SELECT count(*) FROM run_admissions
        WHERE status = 'active'
          AND heartbeat_at > now() - make_interval(mins => %(stale_min)s)) < %(max_conc)s
  AND (SELECT COALESCE(SUM(cost_usd), 0) FROM agent_runs
        WHERE created_at >= current_date) < %(daily_cap)s
  AND NOT EXISTS (
        SELECT 1 FROM research_runs r
         WHERE r.question_hash = %(qhash)s
           AND r.status = 'delivered'
           AND r.delivered_at > now() - make_interval(hours => %(dup_hours)s)
           AND (SELECT count(*) FROM findings f
                 WHERE f.run_id = r.run_id AND f.disposition = 'accepted') >= %(min_find)s)
RETURNING run_id;
```

`force=True` skips only the duplicate clause, never concurrency or budget.

On refusal, run a SEPARATE diagnostic query evaluating the three conditions individually to build the
reason slug. It is racy, but it only decorates an error message — never gate on it. Reason slugs
(stable, tested): `admitted`, `refused_max_concurrency`, `refused_daily_cap`,
`refused_duplicate_of_run_<id>`, `admission_unavailable`.

On `ON CONFLICT (run_id)` — a re-entrant `admit()` for a run that already holds a slot returns
`(True, "admitted")`, so a retried run is not deadlocked by its own prior row.

**`heartbeat`** / **`release`** are fail-SOFT (a missed beat must not kill a healthy run) — swallow
and log to stderr, opposite of `admit`.

Env vars, all read at module level with these defaults:
`MAX_CONCURRENT_RUNS=3`, `DUPLICATE_WINDOW_HOURS=24`, `DUPLICATE_MIN_FINDINGS=8`,
`ADMISSION_HEARTBEAT_STALE_MINUTES=5`, `OPENROUTER_DAILY_CAP_USD=2`.

Follow `pipeline/registry.py` for connection discipline: `psycopg` imported lazily, explicit
`connect_timeout`, `SET statement_timeout` as a statement (NOT as a startup option — Neon's pooled
endpoint rejects `options=-c ...`, lessons #26).

### 3. `tests/test_admission.py`

Model on `tests/test_evidence_budget_sql.py` — execute the PRODUCTION SQL against in-memory sqlite,
translating only placeholder style and the two Postgres-only constructs (`make_interval`, `now()`).
Docstring must state honestly what sqlite can and cannot prove here.

Required cases:
- `question_hash` matches the SQL expression for: trailing whitespace, internal double spaces,
  mixed case, newlines, unicode.
- concurrency: with `max_conc=3` and 3 live rows → refused; with 2 live → admitted.
- stale heartbeat: a row older than `stale_min` does NOT count toward the limit.
- duplicate: prior delivered run, same hash, **18** accepted findings → refused, reason names the run.
- duplicate threshold: prior delivered run, same hash, **3** accepted findings → ADMITTED. (Real case:
  run 43 deliberately re-ran run 35's question because run 35 yielded only 3, and produced 32. The
  gate must not block that.)
- duplicate window: same hash, 18 findings, delivered 40 hours ago → admitted.
- budget: today's `agent_runs` sum ≥ cap → refused; yesterday's spend does not count.
- `force=True` bypasses duplicate but NOT concurrency and NOT budget.

## Files to MODIFY

**NONE.** Deliver the module standalone; wiring it into `run.py` is handled separately.

## Files you MUST NOT TOUCH

`pipeline/run.py`, `pipeline/extract.py`, `collectors/common.py`, `web/app.py`,
`pipeline/report.py`, `pipeline/decompose.py`, `pipeline/synthesize.py`, any existing test,
any existing migration, `docs/`, `.env`, `config/`.

These are being edited concurrently. Touching any of them will cause a conflict and the work will be
rejected.

## Verification

`python -m pytest tests/test_admission.py -v` must pass. Do not run the full suite (other files are
mid-edit). Do not connect to any real database.

## Style

Match the codebase: module docstring explaining WHY the thing exists (see `pipeline/registry.py`,
`pipeline/envfile.py` as models — they explain the incident that motivated them). Comments explain
reasoning, not mechanics. Type hints. No new dependencies.
