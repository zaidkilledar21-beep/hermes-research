-- Hermes Research Engine — Neon Postgres schema (project: hermes-research)
-- Evidence-first design: raw evidence is immutable and content-hashed; every finding
-- in a report must resolve to an evidence_item. Read-only engine — no outreach tables.
-- Apply via Neon MCP run_sql or `psql "$DATABASE_URL" -f schema.sql`.

-- ---------------------------------------------------------------------------
-- research_runs: one row per question the owner submits.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_runs (
    run_id          BIGSERIAL   PRIMARY KEY,
    question        TEXT        NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'decomposing',
                    -- decomposing | collecting | extracting | synthesizing | reviewing | gated | delivered | failed | killed
    submitted_by    TEXT        NOT NULL,          -- telegram owner id
    submitted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at    TIMESTAMPTZ,
    cost_usd        NUMERIC(10,4) NOT NULL DEFAULT 0,
    notes           TEXT,
    synthesis_state TEXT,       -- ok | valid_empty | parse_failed | truncated | schema_invalid
                    -- | transport_failed. ONLY 'valid_empty' may be shown to the owner as
                    -- "no findings" — a parse failure must never masquerade as a negative result.
    synthesis_meta  JSONB,      -- {finish_reason, model, content_len, reasoning_len, errors[], attempts}
    synthesis_raw   TEXT        -- raw model response, so a parse failure stays diagnosable
);

-- ---------------------------------------------------------------------------
-- sub_questions: director decomposes a run into these; each maps to a source plan.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sub_questions (
    sub_id          BIGSERIAL   PRIMARY KEY,
    run_id          BIGINT      NOT NULL REFERENCES research_runs(run_id),
    text            TEXT        NOT NULL,
    source_plan     JSONB       NOT NULL DEFAULT '[]',  -- e.g. ["x","github","reddit","web"]
    status          TEXT        NOT NULL DEFAULT 'pending',
    -- v3 query planner (migration_010): one plan per sub-question, raw kept for diagnosis.
    -- plan_state: planned | fallback_disabled | fallback_budget_cap | fallback_parse_failed
    --             | fallback_schema_invalid | fallback_transport_failed | fallback_truncated
    plan_state      TEXT,
    plan_json       JSONB,
    plan_raw        TEXT,
    plan_model      TEXT,
    -- v3 gap-driven iteration (migration_012)
    round                INT    DEFAULT 0,
    derived_from_finding BIGINT
);

-- ---------------------------------------------------------------------------
-- sources: registry of every source touched, with trust grade + access method.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
    source_id       TEXT        PRIMARY KEY,        -- e.g. 'x_api', 'reddit_reach', 'github_api'
    access_method   TEXT        NOT NULL,           -- official_api | reach_scrape | reader | rss
    is_walled       BOOLEAN     NOT NULL DEFAULT false, -- true = burner/reach, untrusted
    default_grade   CHAR(1)     NOT NULL DEFAULT 'C',   -- A/B/C evidence grade baseline (retrieval fidelity)
    credibility_tier TEXT       NOT NULL DEFAULT 'general_web'
                    -- WHAT KIND of claim this source yields (orthogonal to grade):
                    -- primary_authority | reference | independent_review | vendor_marketing
                    -- | community | general_web | user_supplied
);

-- ---------------------------------------------------------------------------
-- evidence_items: IMMUTABLE. Raw retrieved content, hashed. Never updated in place.
-- Deletions honored via tombstone (content nulled, tombstone_reason set) — hash kept.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evidence_items (
    evidence_id     BIGSERIAL   PRIMARY KEY,
    run_id          BIGINT      NOT NULL REFERENCES research_runs(run_id),
    source_id       TEXT        NOT NULL REFERENCES sources(source_id),
    url             TEXT,
    retrieved_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_hash    TEXT        NOT NULL,           -- sha256 of raw content at retrieval
    content         TEXT,                           -- sanitized RAW text; NULL if tombstoned (immutable)
    extracted       TEXT,                           -- free Nemotron pass: chrome-stripped, claim-preserving; NULL until extracted
    grade           CHAR(1)     NOT NULL DEFAULT 'C',   -- retrieval fidelity (A/B/C)
    trust_tag       TEXT        NOT NULL DEFAULT 'TRUSTED_EVIDENCE',
                    -- walled/reach content = 'UNTRUSTED_EVIDENCE' (injection-quarantined)
    credibility_tier TEXT       NOT NULL DEFAULT 'general_web', -- claim class (see sources table)
    tombstone_reason TEXT,                          -- set on source deletion/opt-out
    UNIQUE (run_id, content_hash)
);

-- ---------------------------------------------------------------------------
-- findings: analyst output. Every finding links to >=1 evidence_item (enforced app-side
-- by the deterministic release gate). label = the honesty classification.
-- ---------------------------------------------------------------------------
-- disposition values include (migration_011): 'superseded_by_revision' — original replaced by a
-- revised/defended row (revision_round=1, parent_finding_id links back). Superseded rows are
-- lineage: every consumer of accepted findings must treat them as invisible.
CREATE TABLE IF NOT EXISTS findings (
    finding_id      BIGSERIAL   PRIMARY KEY,
    run_id          BIGINT      NOT NULL REFERENCES research_runs(run_id),
    claim           TEXT        NOT NULL,
    label           TEXT        NOT NULL,           -- observed | inferred | unknown | community_signal
    confidence      NUMERIC(3,2),                   -- 0.00-1.00, for inferred / community_signal
    evidence_ids    BIGINT[]    NOT NULL DEFAULT '{}',  -- EXACTLY what the model cited, unfiltered:
                    -- fabricated ids are deliberately NOT stripped here, so the gate can detect them
                    -- (stripping them silently turned an integrity violation into a mystery finding)
    contradicts     BIGINT[]    NOT NULL DEFAULT '{}',  -- finding_ids this one conflicts with
    disposition     TEXT        NOT NULL DEFAULT 'accepted',
                    -- accepted | quarantined_no_evidence | quarantined_fabricated_ids
                    -- | quarantined_invalid_label | rejected_by_reviewer  (set by release_gate)
                    -- | superseded_by_revision (set by pipeline/revise.py, preserved by the gate)
    disposition_detail TEXT,
    -- v3 revision loop (migration_011)
    revision_round     INT     DEFAULT 0,
    parent_finding_id  BIGINT  REFERENCES findings(finding_id)
);

-- ---------------------------------------------------------------------------
-- agent_runs: full provenance + cost per model call. Powers budget hard-stop + telemetry.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_runs (
    id              BIGSERIAL   PRIMARY KEY,
    run_id          BIGINT      REFERENCES research_runs(run_id),
    profile         TEXT        NOT NULL,           -- director | scout | analyst
    skill           TEXT,
    model_requested TEXT,
    model_actual    TEXT,
    provider_actual TEXT,
    tokens_in       INT         NOT NULL DEFAULT 0,
    tokens_out      INT         NOT NULL DEFAULT 0,
    cost_usd        NUMERIC(10,6) NOT NULL DEFAULT 0,
    latency_ms      INT,
    schema_valid    BOOLEAN,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- reviews: Claude/Codex CLI reviewer verdicts. Per master-plan §17.5 pattern —
-- bounded, isolated, subscription-backed reviewers. Never a hard gate substitute;
-- release_gate.py (code) is still the only thing that can approve delivery.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reviews (
    review_id       BIGSERIAL   PRIMARY KEY,
    run_id          BIGINT      NOT NULL REFERENCES research_runs(run_id),
    finding_id      BIGINT      REFERENCES findings(finding_id),
    reviewer        TEXT        NOT NULL,           -- 'codex' | 'claude'
    severity        TEXT        NOT NULL DEFAULT 'info',  -- info | flag | reject
    verdict         TEXT        NOT NULL,            -- supported | overreach | contradicted | polished
    detail          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- cross_syntheses: consolidated intelligence brief over MANY delivered runs. Produced by the
-- two-model synth+critic chain (Claude draft -> Codex critique -> Claude revise) in the reviewer
-- container. Distinct artifact from a research_run. Subscription-backed => cost 0.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cross_syntheses (
    synthesis_id    BIGSERIAL   PRIMARY KEY,
    run_ids         BIGINT[]    NOT NULL,           -- the runs consolidated
    title           TEXT,
    status          TEXT        NOT NULL DEFAULT 'synthesizing',  -- synthesizing | delivered | failed
    report_md       TEXT,
    requested_by    TEXT        NOT NULL DEFAULT 'owner',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at    TIMESTAMPTZ,
    cost_usd        NUMERIC(10,4) NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_evidence_run   ON evidence_items(run_id);
CREATE INDEX IF NOT EXISTS idx_findings_run   ON findings(run_id);
CREATE INDEX IF NOT EXISTS idx_cross_syntheses_created ON cross_syntheses(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_subq_run       ON sub_questions(run_id);
CREATE INDEX IF NOT EXISTS idx_agentruns_run  ON agent_runs(run_id);
CREATE INDEX IF NOT EXISTS idx_reviews_run    ON reviews(run_id);

-- Seed the source registry (grade = retrieval fidelity; credibility_tier = claim class).
INSERT INTO sources (source_id, access_method, is_walled, default_grade, credibility_tier) VALUES
    ('x_api',            'official_api', false, 'B', 'community'),
    ('github_api',       'official_api', false, 'A', 'community'),
    ('youtube_dl',       'official_api', false, 'B', 'community'),
    ('rss',              'rss',          false, 'B', 'general_web'),
    ('web_reader',       'reader',       false, 'C', 'general_web'),
    ('web_search',       'reader',       false, 'B', 'general_web'),
    ('hackernews_api',   'official_api', false, 'B', 'community'),
    ('user_doc',         'reader',       false, 'B', 'user_supplied'),
    ('reddit_reach',     'reach_scrape', true,  'C', 'community'),
    ('stackexchange_reach','reach_scrape',true, 'C', 'community'),
    ('trustpilot_reach', 'reach_scrape', true,  'C', 'independent_review'),
    ('forum_reach',      'reach_scrape', true,  'C', 'community'),
    ('instagram_reach',  'reach_scrape', true,  'C', 'community'),
    ('facebook_reach',   'reach_scrape', true,  'C', 'community'),
    -- v3 Part H primary sources (migration_014): the documents an expert cites.
    ('sec_edgar',        'official_api', false, 'A', 'primary_authority'),
    ('courtlistener',    'official_api', false, 'A', 'primary_authority'),
    ('fda_enforcement',  'official_api', false, 'A', 'primary_authority')
ON CONFLICT (source_id) DO NOTHING;
