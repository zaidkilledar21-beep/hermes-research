-- Migration 007 — synthesis failure semantics + per-finding dispositions.
--
-- WHY: two defects destroyed whole valid reports.
--  (1) A JSON parse failure was reported as "no findings produced" — indistinguishable from an
--      honest negative result. Run 20 returned 5197 chars of completed analysis (finish_reason=stop)
--      and the parser silently discarded all of it.
--  (2) The release gate was all-or-nothing: ONE malformed finding hard-blocked run 25's entire
--      ~19-finding report (Shopify AUP, Klaviyo bans, hosting posture — all real, all lost).
--
-- synthesis_state makes "we failed to deserialize" impossible to confuse with "the model found
-- nothing". findings.disposition lets the gate quarantine a bad finding and still deliver the rest,
-- with every disposition visible rather than silently dropped.
-- Apply via Neon MCP or:  psql "$DATABASE_URL" -f db/migration_007_failure_states.sql

ALTER TABLE research_runs
    ADD COLUMN IF NOT EXISTS synthesis_state TEXT;
    -- ok | valid_empty | parse_failed | truncated | schema_invalid | transport_failed
    -- Only 'valid_empty' may be reported to the user as "no findings".

ALTER TABLE research_runs
    ADD COLUMN IF NOT EXISTS synthesis_meta JSONB;
    -- {finish_reason, model, content_len, reasoning_len, validation_errors[], attempts}

ALTER TABLE research_runs
    ADD COLUMN IF NOT EXISTS synthesis_raw TEXT;
    -- raw model response (truncated) so a parse failure is diagnosable after the fact

ALTER TABLE findings
    ADD COLUMN IF NOT EXISTS disposition TEXT NOT NULL DEFAULT 'accepted';
    -- accepted | quarantined_no_evidence | quarantined_fabricated_ids
    -- | quarantined_invalid_label | rejected_by_reviewer

ALTER TABLE findings
    ADD COLUMN IF NOT EXISTS disposition_detail TEXT;
    -- e.g. which evidence ids were fabricated, or the reviewer verdict that rejected it

CREATE INDEX IF NOT EXISTS idx_findings_disposition ON findings(run_id, disposition);
