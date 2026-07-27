-- Migration 008 — comment-level provenance for community evidence.
--
-- WHY: the engine's headline product is practitioner anecdote, but nothing recorded WHO said a
-- thing or WHERE. Without author/thread identity, ten comments in one Reddit thread look identical
-- to ten independent operators reporting the same problem — which is exactly the false corroboration
-- that makes anecdote dangerous. Codex flagged this in review and it is the single most important
-- structural fix for community evidence.
--
-- Corroboration must be counted by DISTINCT author and DISTINCT thread, never by raw item count.
-- Apply via Neon MCP or:  psql "$DATABASE_URL" -f db/migration_008_comment_provenance.sql

ALTER TABLE evidence_items ADD COLUMN IF NOT EXISTS author        TEXT;
ALTER TABLE evidence_items ADD COLUMN IF NOT EXISTS thread_id     TEXT;
ALTER TABLE evidence_items ADD COLUMN IF NOT EXISTS thread_title  TEXT;
ALTER TABLE evidence_items ADD COLUMN IF NOT EXISTS comment_id    TEXT;
ALTER TABLE evidence_items ADD COLUMN IF NOT EXISTS parent_id     TEXT;
ALTER TABLE evidence_items ADD COLUMN IF NOT EXISTS item_score    INT;
ALTER TABLE evidence_items ADD COLUMN IF NOT EXISTS item_kind     TEXT;  -- post | comment | page
ALTER TABLE evidence_items ADD COLUMN IF NOT EXISTS sort_mode     TEXT;  -- top | new | controversial
ALTER TABLE evidence_items ADD COLUMN IF NOT EXISTS depth         INT;

-- Page ownership, recorded separately from credibility_tier. A vendor page is valid evidence of
-- WHAT THE VENDOR PROMISES; it is never evidence that the promise is true. Affiliate/lead-gen pages
-- were a bigger contamination source than obvious vendor blogs in the payments runs.
ALTER TABLE evidence_items ADD COLUMN IF NOT EXISTS page_ownership TEXT;
    -- vendor_owned | affiliate_leadgen | marketplace | sponsored
    -- | independent_commercial | community | unknown

-- Independence lookups: "how many DISTINCT authors back this claim".
CREATE INDEX IF NOT EXISTS idx_evidence_author ON evidence_items(run_id, author);
CREATE INDEX IF NOT EXISTS idx_evidence_thread ON evidence_items(run_id, thread_id);

-- The comment-level Reddit reader (discovery via search engine, reading via the reach container).
INSERT INTO sources (source_id, access_method, is_walled, default_grade, credibility_tier) VALUES
    ('reddit_threads', 'reach_scrape', true, 'C', 'community')
ON CONFLICT (source_id) DO NOTHING;
