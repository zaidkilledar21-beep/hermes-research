-- Migration 012 — gap-driven iteration rounds (v3 Part F).
-- round: 0 = original decomposition; N>0 = follow-up sub-questions derived from round N-1's gaps.
-- derived_from_finding: the 'unknown' (or contradicted) finding this sub-question exists to close.
ALTER TABLE sub_questions
    ADD COLUMN IF NOT EXISTS round INT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS derived_from_finding BIGINT;
