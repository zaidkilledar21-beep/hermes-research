-- Migration 009 — vertical source registry (the engine's cross-run memory of WHERE evidence lives).
--
-- WHY: every run before this one started from zero. Run 28 rediscovered the same generic
-- r/logistics chatter that earlier runs had already found and the relevance filter had already
-- rejected (156 of 166 items), because nothing recorded which venues had ever produced an item
-- that actually answered a question. Discovery targeting cannot improve across runs if the engine
-- has no memory across runs.
--
-- WHAT: one row per (venue, topic). A venue is a subreddit ('r/peptides') or a host
-- ('prepcenter.com'). Usefulness is measured by the extractor's OWN relevance verdict
-- (evidence_items.answers_question), not by item count — the whole point is that item count was
-- already high and useless.
--
-- SAFETY: the registry only ever REORDERS venues that live discovery already returned, and adds at
-- most a couple of site-scoped discovery queries. It can never inject evidence, and a stale or
-- wrong row can only cost read-budget ordering. Nothing here is on the citation path.
--
-- Apply via Neon MCP or:  psql "$DATABASE_URL" -f db/migration_009_vertical_sources.sql

CREATE TABLE IF NOT EXISTS vertical_sources (
    vertical_source_id BIGSERIAL PRIMARY KEY,
    kind         TEXT        NOT NULL,          -- subreddit | site
    identifier   TEXT        NOT NULL,          -- 'r/peptides' | 'prepcenter.com' (lowercased)
    topic_key    TEXT        NOT NULL,          -- human-readable canonical topic ('3pl-peptides-...')
    topic_tokens TEXT[]      NOT NULL DEFAULT '{}',
                 -- the same tokens, unjoined, so a NEW question can match a PAST topic by overlap
                 -- (&&) instead of needing the identical phrasing. Exact-key matching alone would
                 -- make the registry near-useless: no two questions are worded the same way.
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    times_seen   INT         NOT NULL DEFAULT 0,   -- how many runs touched this venue on this topic
    total_items  INT         NOT NULL DEFAULT 0,   -- evidence items retrieved from it
    useful_hits  INT         NOT NULL DEFAULT 0,   -- items the extractor judged answers_question
    useful_runs  INT         NOT NULL DEFAULT 0,
                 -- DISTINCT RUNS in which this venue produced at least one answering item. This,
                 -- not useful_hits, is what promotes a venue: counting items would let two replies
                 -- in ONE thread of ONE run promote a venue, which is the same false-corroboration
                 -- error the evidence layer already fixed by counting distinct authors.
    UNIQUE (kind, identifier, topic_key)
);

-- Ledger of which runs have already been counted into which venue. Without it, re-running
-- `pipeline.registry --run N` (or a retried pipeline stage) adds the same totals again, and a retry
-- alone could manufacture the promotion threshold with no new evidence behind it.
CREATE TABLE IF NOT EXISTS vertical_source_runs (
    vertical_source_id BIGINT NOT NULL REFERENCES vertical_sources(vertical_source_id),
    run_id             BIGINT NOT NULL REFERENCES research_runs(run_id),
    total_items        INT    NOT NULL DEFAULT 0,
    useful_items       INT    NOT NULL DEFAULT 0,
    recorded_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (vertical_source_id, run_id)
);

-- Lookup path: kind + token overlap. GIN makes the && containment check indexable.
CREATE INDEX IF NOT EXISTS idx_vertical_tokens ON vertical_sources USING GIN (topic_tokens);
CREATE INDEX IF NOT EXISTS idx_vertical_kind   ON vertical_sources(kind, useful_runs DESC);
CREATE INDEX IF NOT EXISTS idx_vertical_runs   ON vertical_source_runs(run_id);
