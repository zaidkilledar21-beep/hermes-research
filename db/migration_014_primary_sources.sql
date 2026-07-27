-- Migration 014 — primary-source collectors (v3 Part H): filings, dockets, enforcement records.
INSERT INTO sources (source_id, access_method, is_walled, default_grade, credibility_tier) VALUES
    ('sec_edgar',        'official_api', false, 'A', 'primary_authority'),
    ('courtlistener',    'official_api', false, 'A', 'primary_authority'),
    ('fda_enforcement',  'official_api', false, 'A', 'primary_authority')
ON CONFLICT (source_id) DO NOTHING;
