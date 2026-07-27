"""Tests for figure cross-checking (pipeline/figures.py) and the quote-anchor gate check
(release_gate.quote_anchored) plus synthesize's figure validation — pure, no DB, no network."""
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

from pipeline import figures, release_gate, synthesize  # noqa: E402


class ConflictTests(unittest.TestCase):
    def test_3x_spread_flagged(self):
        rows = [(1, [10], {"value": 99, "unit": "usd/month", "subject": "semaglutide"}),
                (2, [20], {"value": 350, "unit": "usd/month", "subject": "semaglutide"})]
        out = figures.conflicts(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["low"][2], 99)
        self.assertEqual(out[0]["high"][2], 350)

    def test_small_spread_not_flagged(self):
        rows = [(1, [10], {"value": 100, "unit": "usd/month", "subject": "semaglutide"}),
                (2, [20], {"value": 250, "unit": "usd/month", "subject": "semaglutide"})]
        self.assertEqual(figures.conflicts(rows), [])

    def test_different_units_never_compared(self):
        rows = [(1, [10], {"value": 99, "unit": "usd/month", "subject": "semaglutide"}),
                (2, [20], {"value": 400, "unit": "usd", "subject": "semaglutide"})]
        self.assertEqual(figures.conflicts(rows), [])

    def test_different_subjects_never_compared(self):
        rows = [(1, [10], {"value": 99, "unit": "usd/month", "subject": "semaglutide"}),
                (2, [20], {"value": 400, "unit": "usd/month", "subject": "tirzepatide"})]
        self.assertEqual(figures.conflicts(rows), [])

    def test_zero_and_negative_skipped(self):
        rows = [(1, [10], {"value": 0, "unit": "usd", "subject": "fee"}),
                (2, [20], {"value": 500, "unit": "usd", "subject": "fee"})]
        self.assertEqual(figures.conflicts(rows), [])

    def test_malformed_figure_skipped(self):
        rows = [(1, [10], {"value": "lots", "unit": "usd", "subject": "fee"}),
                (2, [20], {"unit": "usd", "subject": "fee"}),
                (3, [30], {"value": 100, "unit": "usd", "subject": "fee"})]
        self.assertEqual(figures.conflicts(rows), [])


class QuoteAnchorTests(unittest.TestCase):
    EVIDENCE = ["They froze my reserve for 90 days with no explanation whatsoever.",
                "Pricing starts at $99/month for the basic Sermorelin program."]

    def test_missing_quote_tolerated(self):
        self.assertIsNone(release_gate.quote_anchored(None, self.EVIDENCE))
        self.assertIsNone(release_gate.quote_anchored("  ", self.EVIDENCE))

    def test_verbatim_quote_passes(self):
        self.assertTrue(release_gate.quote_anchored(
            "froze my reserve for 90 days", self.EVIDENCE))

    def test_case_and_whitespace_normalized(self):
        self.assertTrue(release_gate.quote_anchored(
            "Froze  MY   reserve for 90 days", self.EVIDENCE))

    def test_paraphrase_fails(self):
        self.assertFalse(release_gate.quote_anchored(
            "their reserve was held for three months", self.EVIDENCE))

    def test_too_short_quote_fails(self):
        self.assertFalse(release_gate.quote_anchored("froze", self.EVIDENCE))

    def test_no_evidence_text_fails_supplied_quote(self):
        self.assertFalse(release_gate.quote_anchored("froze my reserve for 90 days", []))


class ValidFiguresTests(unittest.TestCase):
    def test_good_figures_normalized(self):
        out = synthesize._valid_figures([
            {"value": 99, "unit": " USD/Month ", "subject": "Sermorelin Program"}])
        self.assertEqual(out, [{"value": 99, "unit": "usd/month",
                                "subject": "sermorelin-program"}])

    def test_bad_entries_dropped(self):
        out = synthesize._valid_figures([
            "nope", {"value": True, "unit": "usd", "subject": "x"},
            {"value": 10, "unit": "", "subject": "x"},
            {"value": 10, "unit": "usd"},
            {"value": 12.5, "unit": "percent", "subject": "reserve"}])
        self.assertEqual(out, [{"value": 12.5, "unit": "percent", "subject": "reserve"}])

    def test_non_list_returns_empty(self):
        self.assertEqual(synthesize._valid_figures("not a list"), [])
        self.assertEqual(synthesize._valid_figures(None), [])

    def test_capped_at_eight(self):
        many = [{"value": i, "unit": "usd", "subject": f"s{i}"} for i in range(1, 20)]
        self.assertEqual(len(synthesize._valid_figures(many)), 8)


if __name__ == "__main__":
    unittest.main()
