"""Run admission — the layer above a run, which this engine never had.

WHY THIS EXISTS: on 2026-07-28 fifteen `pipeline.run` processes accumulated on the box. Four were
byte-identical re-asks of a question that had already delivered 18 findings; two more duplicated
another. Nothing limited how many runs could exist, nothing noticed a question had already been
answered, and the "daily" budget cap was enforced per run (lessons #34), so fourteen concurrent
workers each correctly believed themselves under budget while the day's ceiling was fourteen times
what the operator had configured.

Every guard in this codebase before this module was per-call or per-run. This one is global.

THREE DESIGN CONSTRAINTS, each learned the hard way:

1. NOT an advisory lock. `DATABASE_URL` points at Neon's POOLED endpoint (PgBouncer, transaction
   mode), so a session-level `pg_advisory_lock` is not pinned to a backend connection across
   transactions. It tests green in a single session, which is exactly what makes it dangerous.
   Same family as lessons #26.

2. NOT read-then-decide. `SELECT count(*)` followed by a Python `if` is a TOCTOU race: under the
   very concurrency this gate exists to control, every worker reads "under limit" and every worker
   proceeds. The check and the reservation are one statement.

3. SERIALIZABLE, because one statement is still not enough. Under the default READ COMMITTED
   isolation two concurrent transactions can share a snapshot, both satisfy
   `(SELECT count(*) ...) < n`, and both insert — the predicate is evaluated against a snapshot
   that does not include the other's uncommitted row. Only SERIALIZABLE makes the count-based
   predicate safe, and it costs a bounded retry loop on serialization failures (SQLSTATE 40001).

FAIL-CLOSED, deliberately. The rest of this codebase is fail-soft by design and says so. Admission
is the exception: if the database is unreachable, `admit()` refuses. A guard that evaporates when
its backing store blips is not a guard, and an ungoverned run is the precise failure being
prevented. `heartbeat()` and `release()` stay fail-soft — a missed bookkeeping write must never
kill a healthy run.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import time

MAX_CONCURRENT_RUNS = int(os.environ.get("MAX_CONCURRENT_RUNS", "3"))
DUPLICATE_WINDOW_HOURS = int(os.environ.get("DUPLICATE_WINDOW_HOURS", "24"))
# A prior run only blocks a re-ask if it actually YIELDED. Run 43 deliberately re-ran run 35's
# question precisely because run 35 returned 3 findings, and produced 32. A legitimate re-run and a
# runaway duplicate look identical except for the prior run's harvest, so that is what we gate on.
DUPLICATE_MIN_FINDINGS = int(os.environ.get("DUPLICATE_MIN_FINDINGS", "8"))
HEARTBEAT_STALE_MINUTES = int(os.environ.get("ADMISSION_HEARTBEAT_STALE_MINUTES", "5"))
DAILY_CAP_USD = float(os.environ.get("OPENROUTER_DAILY_CAP_USD", "2"))
STATEMENT_TIMEOUT_MS = int(os.environ.get("ADMISSION_STATEMENT_TIMEOUT_MS", "10000"))
SERIALIZATION_RETRIES = int(os.environ.get("ADMISSION_SERIALIZATION_RETRIES", "5"))

# Postgres serialization_failure. Under SERIALIZABLE this is the EXPECTED outcome of two runs
# racing for the last slot — it means the gate worked, not that anything is broken.
_SERIALIZATION_FAILURE = "40001"


def question_hash(question: str) -> str:
    """Fingerprint a question for duplicate detection.

    MUST stay byte-identical to the SQL in db/migration_015_run_admission.sql:
        md5(lower(btrim(regexp_replace(question, '\\s+', ' ', 'g'))))
    Order matters: collapse whitespace runs, strip, lowercase, md5. A drift between these two
    would silently disable duplicate detection for every historical row, so it is asserted in
    tests/test_admission.py against the literal SQL expression.
    """
    normalized = re.sub(r"\s+", " ", question or "").strip().lower()
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


def _connect(*, serializable: bool = False):
    """Connect with a bounded statement timeout, set as a STATEMENT rather than a startup option —
    `options=-c statement_timeout=...` is rejected outright by Neon's pooled endpoint (lessons #26,
    same reasoning as pipeline/registry.py)."""
    import psycopg  # lazy: keeps this module importable without a database for unit tests

    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=not serializable,
                           connect_timeout=10)
    try:
        conn.execute(f"SET statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
        if serializable:
            conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
    except Exception as e:                                    # pragma: no cover - defensive
        print(f"[admission] session setup partial: {type(e).__name__}: {e}", file=sys.stderr)
    return conn


# One statement decides everything. Zero rows returned == refused; there is no separate "check"
# phase that another worker could interleave with.
#
# The `NOT EXISTS (... run_admissions ...)` re-entry clause lets a run that ALREADY holds a slot
# pass even when the pool is full — otherwise a retried or resumed run would be locked out by its
# own reservation.
ADMIT_SQL = """
INSERT INTO run_admissions (run_id, question_hash)
SELECT %(run_id)s, %(qhash)s
WHERE (
        EXISTS (SELECT 1 FROM run_admissions WHERE run_id = %(run_id)s AND status = 'active')
        OR (SELECT count(*) FROM run_admissions
             WHERE status = 'active'
               AND heartbeat_at > now() - make_interval(mins => %(stale_min)s)) < %(max_conc)s
      )
  AND (SELECT COALESCE(SUM(cost_usd), 0) FROM agent_runs
        WHERE created_at >= current_date) < %(daily_cap)s
  AND (%(force)s OR NOT EXISTS (
        SELECT 1 FROM research_runs r
         WHERE r.question_hash = %(qhash)s
           AND r.run_id <> %(run_id)s
           AND r.status = 'delivered'
           AND r.delivered_at > now() - make_interval(hours => %(dup_hours)s)
           AND (SELECT count(*) FROM findings f
                 WHERE f.run_id = r.run_id AND f.disposition = 'accepted') >= %(min_find)s))
ON CONFLICT (run_id) DO UPDATE
    SET heartbeat_at = now(), status = 'active'
RETURNING run_id
"""

# Only ever used to DECORATE a refusal message. Racy by construction and never gated on — by the
# time it runs, the authoritative decision has already been made and committed above.
_DIAGNOSE_SQL = """
SELECT
  (SELECT count(*) FROM run_admissions
    WHERE status = 'active'
      AND heartbeat_at > now() - make_interval(mins => %(stale_min)s)) AS live,
  (SELECT COALESCE(SUM(cost_usd), 0) FROM agent_runs WHERE created_at >= current_date) AS spent,
  (SELECT r.run_id FROM research_runs r
    WHERE r.question_hash = %(qhash)s
      AND r.run_id <> %(run_id)s
      AND r.status = 'delivered'
      AND r.delivered_at > now() - make_interval(hours => %(dup_hours)s)
      AND (SELECT count(*) FROM findings f
            WHERE f.run_id = r.run_id AND f.disposition = 'accepted') >= %(min_find)s
    ORDER BY r.delivered_at DESC LIMIT 1) AS dup_run
"""


def _params(run_id: int, qhash: str, force: bool) -> dict:
    return {"run_id": run_id, "qhash": qhash, "force": force,
            "stale_min": HEARTBEAT_STALE_MINUTES, "max_conc": MAX_CONCURRENT_RUNS,
            "daily_cap": DAILY_CAP_USD, "dup_hours": DUPLICATE_WINDOW_HOURS,
            "min_find": DUPLICATE_MIN_FINDINGS}


def admit(run_id: int, question: str, *, force: bool = False) -> tuple[bool, str]:
    """Reserve a slot for this run. Returns (admitted, reason_slug).

    Reasons: `admitted`, `refused_max_concurrency`, `refused_daily_cap`,
    `refused_duplicate_of_run_<id>`, `admission_unavailable`.

    force=True waives the DUPLICATE check only — never concurrency, never budget. An operator
    re-running a thin question is a real workflow; an operator wanting to exceed the spend ceiling
    should change the ceiling.
    """
    qhash = question_hash(question)
    params = _params(run_id, qhash, force)

    for attempt in range(1, SERIALIZATION_RETRIES + 1):
        conn = None
        try:
            conn = _connect(serializable=True)
            row = conn.execute(ADMIT_SQL, params).fetchone()
            conn.commit()
            if row:
                return True, "admitted"
            break                                    # cleanly refused; go describe why
        except Exception as exc:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            if getattr(exc, "sqlstate", None) == _SERIALIZATION_FAILURE:
                # Two runs raced for the same slot and Postgres serialized them. Expected.
                if attempt < SERIALIZATION_RETRIES:
                    time.sleep(0.05 * attempt)
                    continue
                return False, "refused_max_concurrency"
            print(f"[admission] unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
            return False, "admission_unavailable"
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    return False, _describe_refusal(run_id, qhash)


def _describe_refusal(run_id: int, qhash: str) -> str:
    try:
        with _connect() as conn:
            live, spent, dup_run = conn.execute(
                _DIAGNOSE_SQL, _params(run_id, qhash, False)).fetchone()
    except Exception:
        return "refused_unknown"
    if dup_run is not None:
        return f"refused_duplicate_of_run_{dup_run}"
    if float(spent or 0) >= DAILY_CAP_USD:
        return "refused_daily_cap"
    if int(live or 0) >= MAX_CONCURRENT_RUNS:
        return "refused_max_concurrency"
    return "refused_unknown"


def heartbeat(run_id: int) -> None:
    """Refresh this run's reservation. Called at every stage boundary. Fail-soft: a missed beat
    costs nothing until HEARTBEAT_STALE_MINUTES of them are missed in a row, at which point the
    run is genuinely gone and its slot SHOULD be reclaimed."""
    try:
        with _connect() as conn:
            conn.execute("UPDATE run_admissions SET heartbeat_at = now() WHERE run_id = %s",
                         (run_id,))
    except Exception as e:
        print(f"[admission] heartbeat skipped for run {run_id}: {type(e).__name__}: {e}",
              file=sys.stderr)


def release(run_id: int, status: str = "finished") -> None:
    """Free the slot. Fail-soft — if this never lands, the stale-heartbeat sweep reclaims the slot
    within HEARTBEAT_STALE_MINUTES anyway. That redundancy is the point: releasing is an
    optimization, expiry is the guarantee."""
    try:
        with _connect() as conn:
            conn.execute("UPDATE run_admissions SET status = %s WHERE run_id = %s",
                         (status, run_id))
    except Exception as e:
        print(f"[admission] release skipped for run {run_id}: {type(e).__name__}: {e}",
              file=sys.stderr)
