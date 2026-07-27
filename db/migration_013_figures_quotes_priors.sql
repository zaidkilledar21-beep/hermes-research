-- Migration 013 — quote-anchored citations + structured figures (v3 Parts G/M1) and the
-- fact-level vertical memory table (Part L).

-- findings.quote: a short verbatim span from the cited evidence supporting an 'observed' claim.
-- The gate string-matches it (normalized) against the cited text — fabrication detection moves
-- from id-existence to content level. Missing quote is tolerated (transition period); a quote
-- that does not appear in the cited evidence quarantines the finding.
-- findings.figures: [{"value": 99, "unit": "usd/month", "subject": "sermorelin-program"}] —
-- consumed by pipeline/figures.py (cross-check) and pipeline/priors.py (vertical memory).
ALTER TABLE findings
    ADD COLUMN IF NOT EXISTS quote   TEXT,
    ADD COLUMN IF NOT EXISTS figures JSONB;

-- vertical_facts: the registry pattern extended from venues to figures. Accepted findings' figures
-- accumulate per vertical; once >= PRIORS_MIN_N observations exist for (vertical, subject, unit),
-- new figures deviating > PRIORS_SURPRISE_FACTOR x from the stored median generate a flag finding.
-- Priors NEVER auto-reject — they only flag, and reviewers judge the flag like any finding.
CREATE TABLE IF NOT EXISTS vertical_facts (
    fact_id     BIGSERIAL PRIMARY KEY,
    vertical    TEXT NOT NULL,
    subject     TEXT NOT NULL,
    unit        TEXT NOT NULL,
    value       NUMERIC NOT NULL,
    run_id      BIGINT NOT NULL,
    finding_id  BIGINT NOT NULL,
    observed_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (finding_id, subject, unit)   -- idempotent per finding
);
CREATE INDEX IF NOT EXISTS vf_lookup ON vertical_facts (vertical, subject, unit);
