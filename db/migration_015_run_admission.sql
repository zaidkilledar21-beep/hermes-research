-- Migration 015 - atomic run admission.
--
-- WHY: pipeline.run can be started independently by both web entry points and by an agent shell.
-- Fifteen bare workers accumulated in production, including four byte-identical repeats of a
-- question that had already delivered 18 findings. The admission ledger gives every worker one
-- database-backed reservation to acquire before it can do work. Heartbeats make abandoned
-- reservations expire without relying on process-local state or session-level advisory locks,
-- which are unsafe behind Neon's transaction-pooled endpoint.

-- Store the same normalized fingerprint used by pipeline/admission.py. Backfilling makes duplicate
-- protection effective for delivered runs that predate the gate.
ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS question_hash text;

UPDATE research_runs
   SET question_hash = md5(lower(btrim(regexp_replace(question, '\s+', ' ', 'g'))))
 WHERE question_hash IS NULL;

-- Duplicate checks filter by fingerprint and delivery state on every attempted admission.
CREATE INDEX IF NOT EXISTS idx_research_runs_qhash
    ON research_runs (question_hash, status);

-- This table is the durable slot ledger. A run_id is also its idempotency key, so retrying the same
-- run can refresh its reservation instead of competing with itself for another slot.
CREATE TABLE IF NOT EXISTS run_admissions (
    run_id        integer PRIMARY KEY,
    question_hash text        NOT NULL,
    started_at    timestamptz NOT NULL DEFAULT now(),
    heartbeat_at  timestamptz NOT NULL DEFAULT now(),
    status        text        NOT NULL DEFAULT 'active'
);

-- Live-slot counts ignore abandoned workers once their heartbeat crosses the configured stale age.
CREATE INDEX IF NOT EXISTS idx_run_admissions_live
    ON run_admissions (status, heartbeat_at);
