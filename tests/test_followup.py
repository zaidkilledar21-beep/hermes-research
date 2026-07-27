"""Tests for gap-driven iteration (pipeline/followup.py) — pure, no DB, no network."""
import json
import os
import sys
import types
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://unused")
os.environ.setdefault("OPENROUTER_API_KEY_ANALYST", "unused")

if "psycopg" not in sys.modules:
    stub = types.ModuleType("psycopg")
    stub.connect = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no database in tests"))
    sys.modules["psycopg"] = stub

from pipeline import followup  # noqa: E402

GAPS = {
    "unknowns": [{"finding_id": 3, "claim": "Evidence does not establish X's licensing status"}],
    "contradictions": [{"finding_id": 5, "claim": "Vendor says 99% same-day shipping",
                        "conflicts_with": 6,
                        "other_claim": "Three operators report multi-week delays"}],
}
EXISTING = ["What is vendor X's licensing status in California?"]


def _raw(subqs):
    return json.dumps({"sub_questions": subqs})


class ParseSubqsTests(unittest.TestCase):
    def test_valid_subq_kept_with_derivation(self):
        out = followup._parse_subqs(_raw([
            {"text": "Which state boards list vendor X's pharmacy license or actions against it?",
             "source_plan": ["web_search"], "derived_from_finding": 3}]), GAPS, EXISTING)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["derived_from_finding"], 3)
        self.assertEqual(out[0]["source_plan"], ["web_search"])

    def test_repeat_of_existing_subq_dropped(self):
        out = followup._parse_subqs(_raw([
            {"text": "What is vendor X's licensing status in California?",
             "source_plan": ["web_search"], "derived_from_finding": 3}]), GAPS, EXISTING)
        self.assertEqual(out, [])

    def test_repeat_detection_is_normalized(self):
        out = followup._parse_subqs(_raw([
            {"text": "  WHAT is vendor  x's licensing status in california?  ",
             "source_plan": ["web_search"], "derived_from_finding": 3}]), GAPS, EXISTING)
        self.assertEqual(out, [])

    def test_disallowed_sources_replaced_with_default(self):
        out = followup._parse_subqs(_raw([
            {"text": "Court records naming vendor X in fulfillment disputes since 2024",
             "source_plan": ["instagram_reach", "facebook_reach"], "derived_from_finding": 5}]),
            GAPS, EXISTING)
        self.assertEqual(out[0]["source_plan"], followup.DEFAULT_SOURCES)

    def test_unknown_derivation_id_nulled(self):
        out = followup._parse_subqs(_raw([
            {"text": "Independent audits of vendor X's shipping performance claims",
             "source_plan": ["web_search"], "derived_from_finding": 999}]), GAPS, EXISTING)
        self.assertIsNone(out[0]["derived_from_finding"])

    def test_cap_enforced(self):
        subqs = [{"text": f"A sufficiently long follow-up question number {i} about the topic?",
                  "source_plan": ["web_search"], "derived_from_finding": 3} for i in range(10)]
        out = followup._parse_subqs(_raw(subqs), GAPS, EXISTING)
        self.assertEqual(len(out), followup.MAX_SUBQS)

    def test_too_short_text_dropped(self):
        out = followup._parse_subqs(_raw([
            {"text": "short", "source_plan": ["web_search"]}]), GAPS, EXISTING)
        self.assertEqual(out, [])

    def test_garbage_returns_empty(self):
        self.assertEqual(followup._parse_subqs("not json", GAPS, EXISTING), [])
        self.assertEqual(followup._parse_subqs('{"wrong": []}', GAPS, EXISTING), [])

    def test_duplicate_new_subqs_deduped(self):
        subqs = [{"text": "Which regulators have acted against vendor X since 2024?",
                  "source_plan": ["web_search"], "derived_from_finding": 3},
                 {"text": "which regulators have acted against vendor x since 2024?",
                  "source_plan": ["reddit_threads"], "derived_from_finding": 5}]
        out = followup._parse_subqs(_raw(subqs), GAPS, EXISTING)
        self.assertEqual(len(out), 1)


class ClosureTests(unittest.TestCase):
    def test_all_closed(self):
        self.assertEqual(followup.closure(4, 0), 1.0)

    def test_none_closed(self):
        self.assertEqual(followup.closure(4, 4), 0.0)

    def test_gaps_grew_floors_at_zero(self):
        self.assertEqual(followup.closure(2, 5), 0.0)

    def test_no_prior_unknowns_counts_as_closed(self):
        self.assertEqual(followup.closure(0, 3), 1.0)

    def test_partial(self):
        self.assertEqual(followup.closure(4, 1), 0.75)


if __name__ == "__main__":
    unittest.main()
