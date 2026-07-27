-- Migration 004 — register the open-web search source.
-- web_search: SearXNG finds relevant URLs by keyword, then the existing Jina reader pulls each
-- page's content. This is the "search the web" spine the engine was missing (only had "read a
-- given URL"). Trusted (self-hosted search over public web), general_web tier, grade B.
-- Apply via Neon MCP or:  psql "$DATABASE_URL" -f db/migration_004_websearch.sql

INSERT INTO sources (source_id, access_method, is_walled, default_grade, credibility_tier) VALUES
    ('web_search', 'reader', false, 'B', 'general_web')
ON CONFLICT (source_id) DO NOTHING;
