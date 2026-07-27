-- Migration 005 — cross-run synthesizer output table.
-- A cross_synthesis is a consolidated intelligence brief over MULTIPLE delivered runs (e.g. the
-- 3PL + payments + compliance runs for one business), produced by the two-model synth+critic chain
-- (Claude Opus draft -> Codex critique -> Claude revise) in the reviewer container. Subscription-
-- backed, $0. Distinct artifact from a research_run, so its own table.
-- Apply via Neon MCP or:  psql "$DATABASE_URL" -f db/migration_005_cross_synthesis.sql

CREATE TABLE IF NOT EXISTS cross_syntheses (
    synthesis_id    BIGSERIAL   PRIMARY KEY,
    run_ids         BIGINT[]    NOT NULL,           -- the runs consolidated
    title           TEXT,
    status          TEXT        NOT NULL DEFAULT 'synthesizing',  -- synthesizing | delivered | failed
    report_md       TEXT,                           -- final brief (Claude revision + Codex critique appendix)
    requested_by    TEXT        NOT NULL DEFAULT 'owner',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at    TIMESTAMPTZ,
    cost_usd        NUMERIC(10,4) NOT NULL DEFAULT 0 -- 0: both CLIs are subscription-backed
);

CREATE INDEX IF NOT EXISTS idx_cross_syntheses_created ON cross_syntheses(created_at DESC);
