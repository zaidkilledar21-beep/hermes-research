"""Tests for fact-level vertical memory (pipeline/priors.py) — pure parts only."""
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

from pipeline import priors  # noqa: E402


class SurprisingTests(unittest.TestCase):
    def test_within_factor_not_surprising(self):
        self.assertFalse(priors.surprising(250, 100, factor=3))
        self.assertFalse(priors.surprising(100, 250, factor=3))
        self.assertFalse(priors.surprising(100, 100, factor=3))

    def test_above_factor_surprising_both_directions(self):
        self.assertTrue(priors.surprising(400, 100, factor=3))
        self.assertTrue(priors.surprising(100, 400, factor=3))

    def test_exactly_at_factor_not_surprising(self):
        self.assertFalse(priors.surprising(300, 100, factor=3))

    def test_zero_or_negative_never_judged(self):
        self.assertFalse(priors.surprising(100, 0, factor=3))
        self.assertFalse(priors.surprising(0, 100, factor=3))
        self.assertFalse(priors.surprising(-50, 100, factor=3))


if __name__ == "__main__":
    unittest.main()
