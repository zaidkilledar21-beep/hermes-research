-- Migration 011 — revision loop lineage (v3 Part E).
-- revision_round: 0 = original synthesis output; 1 = produced by the revise pass.
-- parent_finding_id: the rejected finding a revision replaces (originals get
--   disposition='superseded_by_revision' — a TEXT convention, no enum; every consumer of accepted
--   findings must treat superseded as invisible, see pipeline/revise.py's consumer audit note).
ALTER TABLE findings
    ADD COLUMN IF NOT EXISTS revision_round    INT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS parent_finding_id BIGINT REFERENCES findings(finding_id);
