"""Tests for the revision loop (pipeline/revise.py) — pure, no DB, no network.

The two contracts that matter: (1) a defence without a quote that actually appears in the cited
evidence is demoted to drop — the model never wins an argument by assertion; (2) with
MAX_REVISION_ROUNDS=0 (the shipping default) revise_run touches nothing at all.
"""
import json
import os
import sys
import types
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_URL", "postgresql://unused")
os.environ.setdefault("OPENROUTER_API_KEY_ANALYST", "unused")

if "psycopg" not in sys.modules:
    stub = types.ModuleType("psycopg")
    stub.connect = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no database in tests"))
    sys.modules["psycopg"] = stub

from pipeline import revise  # noqa: E402

REJECTED = [
    {"finding_id": 7, "claim": "Vendor X freezes reserves for 90 days", "label": "community_signal",
     "confidence": 0.6,
     "evidence": [{"id": 41, "text": "They froze my reserve for 90 days with no explanation, "
                                     "then released it after I threatened a chargeback."}],
     "critiques": [{"reviewer": "codex", "critique": "single anonymous report"}]},
    {"finding_id": 9, "claim": "Vendor X is FDA registered", "label": "observed",
     "confidence": None,
     "evidence": [{"id": 44, "text": "Their site claims FDA registration but the FDA database "
                                     "has no matching entry."}],
     "critiques": [{"reviewer": "claude", "critique": "evidence contradicts the claim"}]},
]


def _raw(revisions):
    return json.dumps({"revisions": revisions})


class ParseRevisionsTests(unittest.TestCase):
    def test_grounded_defence_kept(self):
        out, errors = revise.parse_revisions(_raw([
            {"finding_id": 7, "action": "defend",
             "quote": "froze my reserve for 90 days", "reason": "quote supports it"}]), REJECTED)
        self.assertEqual(out[0]["action"], "defend")
        self.assertEqual(out[0]["claim"], REJECTED[0]["claim"])  # defence keeps original claim
        self.assertEqual(out[0]["evidence_ids"], [41])

    def test_ungrounded_defence_demoted_to_drop(self):
        out, errors = revise.parse_revisions(_raw([
            {"finding_id": 7, "action": "defend",
             "quote": "this text appears nowhere in the evidence at all"}]), REJECTED)
        self.assertEqual(out[0]["action"], "drop")
        self.assertTrue(any("not grounded" in e for e in errors))

    def test_defence_quote_normalized_matching(self):
        # case + whitespace differences must not defeat a genuinely verbatim quote
        out, _ = revise.parse_revisions(_raw([
            {"finding_id": 7, "action": "defend",
             "quote": "Froze  my   RESERVE for 90 days"}]), REJECTED)
        self.assertEqual(out[0]["action"], "defend")

    def test_short_quote_rejected(self):
        out, _ = revise.parse_revisions(_raw([
            {"finding_id": 7, "action": "defend", "quote": "froze"}]), REJECTED)
        self.assertEqual(out[0]["action"], "drop")

    def test_revise_with_empty_claim_demoted(self):
        out, errors = revise.parse_revisions(_raw([
            {"finding_id": 9, "action": "revise", "claim": "  "}]), REJECTED)
        self.assertEqual(out[0]["action"], "drop")

    def test_revise_keeps_original_label_when_invalid(self):
        out, _ = revise.parse_revisions(_raw([
            {"finding_id": 9, "action": "revise",
             "claim": "Vendor X claims FDA registration; the FDA database has no matching entry",
             "label": "banana", "evidence_ids": [44]}]), REJECTED)
        self.assertEqual(out[0]["label"], "observed")

    def test_confidence_only_for_probabilistic_labels(self):
        out, _ = revise.parse_revisions(_raw([
            {"finding_id": 9, "action": "revise", "claim": "Site claims registration",
             "label": "observed", "confidence": 0.9, "evidence_ids": [44]}]), REJECTED)
        self.assertIsNone(out[0]["confidence"])

    def test_unknown_finding_id_dropped(self):
        out, errors = revise.parse_revisions(_raw([
            {"finding_id": 999, "action": "drop"}]), REJECTED)
        self.assertEqual(out, [])
        self.assertTrue(any("unknown" in e for e in errors))

    def test_duplicate_finding_id_second_ignored(self):
        out, _ = revise.parse_revisions(_raw([
            {"finding_id": 7, "action": "drop"},
            {"finding_id": 7, "action": "defend", "quote": "froze my reserve for 90 days"}]),
            REJECTED)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "drop")

    def test_fenced_json_parsed(self):
        raw = "```json\n" + _raw([{"finding_id": 7, "action": "drop"}]) + "\n```"
        out, _ = revise.parse_revisions(raw, REJECTED)
        self.assertEqual(out[0]["action"], "drop")

    def test_garbage_returns_empty_with_errors(self):
        out, errors = revise.parse_revisions("not json at all", REJECTED)
        self.assertEqual(out, [])
        self.assertTrue(errors)

    def test_invalid_evidence_ids_fall_back_to_cited(self):
        out, _ = revise.parse_revisions(_raw([
            {"finding_id": 7, "action": "revise", "claim": "One operator reports a 90-day freeze",
             "label": "community_signal", "confidence": 0.5, "evidence_ids": ["nope", None]}]),
            REJECTED)
        self.assertEqual(out[0]["evidence_ids"], [41])


class ReviseRunGuardTests(unittest.TestCase):
    def test_disabled_by_default_touches_nothing(self):
        # MAX_ROUNDS=0 is the shipping default; revise_run must return without any DB/model call.
        with mock.patch.object(revise, "MAX_ROUNDS", 0), \
             mock.patch.object(revise, "load_rejected",
                               side_effect=AssertionError("must not query")) as loader:
            counts = revise.revise_run(1, "q")
        self.assertEqual(counts["rejected"], 0)
        loader.assert_not_called()

    def test_zero_rejects_short_circuits_before_model(self):
        with mock.patch.object(revise, "MAX_ROUNDS", 1), \
             mock.patch.object(revise, "load_rejected", return_value=[]), \
             mock.patch.object(revise, "_call_model",
                               side_effect=AssertionError("must not call model")):
            counts = revise.revise_run(1, "q")
        self.assertEqual(counts["rejected"], 0)

    def test_budget_cap_short_circuits_before_model(self):
        common_stub = types.SimpleNamespace(budget_spent=lambda run_id: 99.0,
                                            log_agent_run=lambda *a, **k: None)
        with mock.patch.object(revise, "MAX_ROUNDS", 1), \
             mock.patch.object(revise, "load_rejected", return_value=REJECTED), \
             mock.patch.dict(sys.modules, {"collectors": types.SimpleNamespace(common=common_stub),
                                           "collectors.common": common_stub}), \
             mock.patch.object(revise, "_call_model",
                               side_effect=AssertionError("must not call model")):
            counts = revise.revise_run(1, "q")
        self.assertEqual(counts["rejected"], 2)
        self.assertEqual(counts["new_ids"], [])


if __name__ == "__main__":
    unittest.main()
