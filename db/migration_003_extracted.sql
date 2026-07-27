-- Migration 003 — Nemotron bulk-extraction column.
-- `content` stays IMMUTABLE (raw sanitized retrieval, the audit/citation source of truth).
-- `extracted` holds the free Nemotron pass's cleaned, chrome-stripped, claim-preserving version;
-- NULL until extracted (fail-soft: an item that never got extracted just falls back to content).
-- Apply via Neon MCP or:  psql "$DATABASE_URL" -f db/migration_003_extracted.sql

ALTER TABLE evidence_items
    ADD COLUMN IF NOT EXISTS extracted TEXT;
