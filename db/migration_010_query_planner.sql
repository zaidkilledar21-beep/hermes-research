-- Migration 010 — LLM query planner state, one plan per sub-question (v3 Part A).
-- Mirrors research_runs.synthesis_state/synthesis_meta/synthesis_raw: a structured column for
-- consumers (run.py, evals) plus the raw model response so a parse failure stays diagnosable.
-- plan_model is denormalized on purpose: bakeoff inspection without joining agent_runs.
ALTER TABLE sub_questions
    ADD COLUMN IF NOT EXISTS plan_state TEXT,
    ADD COLUMN IF NOT EXISTS plan_json  JSONB,
    ADD COLUMN IF NOT EXISTS plan_raw   TEXT,
    ADD COLUMN IF NOT EXISTS plan_model TEXT;
-- plan_state values: planned | fallback_disabled | fallback_budget_cap | fallback_parse_failed
--                    | fallback_schema_invalid | fallback_transport_failed | fallback_truncated
