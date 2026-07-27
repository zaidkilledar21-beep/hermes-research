-- Migration 002 — credibility tiering + new community/review sources.
-- Adds a claim-CLASS dimension (credibility_tier) orthogonal to the existing A/B/C retrieval
-- grade, so synthesis can say "vendor claims X but N independent forum users report Y" instead
-- of averaging every text blob together. Backward-compatible: nullable-with-default column,
-- existing rows keep working. Apply via Neon MCP run_sql or:
--   psql "$DATABASE_URL" -f db/migration_002_credibility_tier.sql

-- 1. New column on the source registry: the baseline tier for content from that source.
ALTER TABLE sources
    ADD COLUMN IF NOT EXISTS credibility_tier TEXT NOT NULL DEFAULT 'general_web';
    -- primary_authority | reference | independent_review | vendor_marketing
    -- | community | general_web | user_supplied

-- 2. New column on evidence: the resolved tier for THIS item (source baseline, or a
--    per-item override such as the web-domain heuristic for gov/edu authority pages).
ALTER TABLE evidence_items
    ADD COLUMN IF NOT EXISTS credibility_tier TEXT NOT NULL DEFAULT 'general_web';

-- 3. Register the new community/review reader sources + a user-supplied document source.
INSERT INTO sources (source_id, access_method, is_walled, default_grade, credibility_tier) VALUES
    ('stackexchange_reach', 'reach_scrape', true,  'C', 'community'),
    ('trustpilot_reach',    'reach_scrape', true,  'C', 'independent_review'),
    ('forum_reach',         'reach_scrape', true,  'C', 'community'),
    ('hackernews_api',      'official_api', false, 'B', 'community'),
    ('user_doc',            'reader',       false, 'B', 'user_supplied')
ON CONFLICT (source_id) DO NOTHING;

-- 4. Backfill baseline tiers on the existing source registry.
UPDATE sources SET credibility_tier = 'community'   WHERE source_id IN
    ('x_api','github_api','youtube_dl','reddit_reach','instagram_reach','facebook_reach');
UPDATE sources SET credibility_tier = 'general_web' WHERE source_id IN ('rss','web_reader');

-- 5. Backfill tier on already-stored evidence from its source baseline (best-effort; new
--    items get tiered at ingest by common.store_evidence).
UPDATE evidence_items e
   SET credibility_tier = s.credibility_tier
  FROM sources s
 WHERE e.source_id = s.source_id
   AND e.credibility_tier = 'general_web';

CREATE INDEX IF NOT EXISTS idx_evidence_tier ON evidence_items(run_id, credibility_tier);
