"""Tests for question decomposition (pipeline/decompose.py) — pure, no DB, no network.

The fail-soft contract is the one that matters: decompose() must return exactly the pre-existing
single-sub-question shape, [(question, [])], on every failure path — disabled, budget cap,
transport, parse, schema, truncation. Nothing calling request_run() should ever see a difference
between "decompose is off" and "decompose tried and failed".
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

from pipeline import decompose  # noqa: E402

GOOD = {
    "sub_questions": [
        {"text": "What does it cost to launch a peptide e-commerce store?", "extra_sources": []},
        {"text": "What licensing does peptide e-commerce require by state?",
         "extra_sources": ["fda_enforcement"]},
        {"text": "Who are the established competitors and their pricing?", "extra_sources": []},
    ]
}


def _msg(content, finish="stop"):
    return {"content": content}, finish


class ClassifyTests(unittest.TestCase):
    def test_clean_json_ok(self):
        subqs, state, _ = decompose.classify_response(*_msg(json.dumps(GOOD)))
        self.assertEqual(state, "ok")
        self.assertEqual(len(subqs), 3)

    def test_fenced_json_ok(self):
        raw = "```json\n" + json.dumps(GOOD) + "\n```"
        subqs, state, _ = decompose.classify_response(*_msg(raw))
        self.assertEqual(state, "ok")

    def test_missing_key_is_schema_invalid(self):
        subqs, state, _ = decompose.classify_response(*_msg('{"facets": []}'))
        self.assertIsNone(subqs)
        self.assertEqual(state, "schema_invalid")

    def test_truncated(self):
        subqs, state, _ = decompose.classify_response({"content": '{"sub_q'}, "length")
        self.assertIsNone(subqs)
        self.assertEqual(state, "truncated")

    def test_empty_is_parse_failed(self):
        subqs, state, _ = decompose.classify_response({"content": ""}, "stop")
        self.assertEqual(state, "parse_failed")


class ValidateTests(unittest.TestCase):
    def test_caps_at_five(self):
        many = {"sub_questions": [{"text": f"Question number {i} about something specific?",
                                   "extra_sources": []} for i in range(10)]}
        out = decompose._validate(many)
        self.assertEqual(len(out), decompose.MAX_SUBQS)

    def test_invalid_extra_source_dropped(self):
        out = decompose._validate({"sub_questions": [
            {"text": "A real sub-question about pricing here?",
             "extra_sources": ["not_a_real_source", "fda_enforcement"]}]})
        self.assertEqual(out[0]["extra_sources"], ["fda_enforcement"])

    def test_short_text_dropped(self):
        out = decompose._validate({"sub_questions": [{"text": "short"}]})
        self.assertIsNone(out)

    def test_duplicate_facets_deduped(self):
        out = decompose._validate({"sub_questions": [
            {"text": "What does it cost to launch?", "extra_sources": []},
            {"text": "what does it cost to launch?", "extra_sources": []},
        ]})
        self.assertEqual(len(out), 1)

    def test_all_bad_returns_none(self):
        self.assertIsNone(decompose._validate({"sub_questions": ["nope", {"text": ""}]}))
        self.assertIsNone(decompose._validate({"sub_questions": "not a list"}))
        self.assertIsNone(decompose._validate("not a dict"))


class DecomposeFailSoftTests(unittest.TestCase):
    """The contract that matters: every failure path returns [(question, [])]."""

    def test_disabled_returns_fallback_without_network(self):
        with mock.patch.object(decompose, "ENABLED", False), \
             mock.patch.object(decompose, "_call_model",
                               side_effect=AssertionError("must not call model")):
            subqs, tele = decompose.decompose("Some question?")
        self.assertEqual(subqs, [("Some question?", [])])
        self.assertEqual(tele["label"], "disabled")

    def test_budget_cap_returns_fallback_without_network(self):
        common_stub = types.SimpleNamespace(budget_spent=lambda run_id: 99.0)
        with mock.patch.object(decompose, "ENABLED", True), \
             mock.patch.dict(sys.modules, {"collectors": types.SimpleNamespace(common=common_stub),
                                           "collectors.common": common_stub}), \
             mock.patch.object(decompose, "_call_model",
                               side_effect=AssertionError("must not call model")):
            subqs, tele = decompose.decompose("Some question?", run_id=1)
        self.assertEqual(subqs, [("Some question?", [])])
        self.assertEqual(tele["label"], "fallback_budget_cap")

    def test_transport_failure_falls_back(self):
        with mock.patch.object(decompose, "ENABLED", True), \
             mock.patch.object(decompose, "_call_model", side_effect=RuntimeError("boom")):
            subqs, tele = decompose.decompose("Some question?")
        self.assertEqual(subqs, [("Some question?", [])])
        self.assertTrue(tele["label"].startswith("fallback_transport_failed"))

    def test_successful_decomposition(self):
        usage = {"model": "tencent/hy3-20260706", "prompt_tokens": 100, "completion_tokens": 50,
                 "cost": 0.001, "_msg": {"content": json.dumps(GOOD)}, "_finish": "stop"}
        with mock.patch.object(decompose, "ENABLED", True), \
             mock.patch.object(decompose, "_call_model",
                               return_value=(json.dumps(GOOD), usage)):
            subqs, tele = decompose.decompose("What does it take to run this business?")
        self.assertEqual(len(subqs), 3)
        self.assertEqual(subqs[1][1], ["fda_enforcement"])
        self.assertTrue(tele["label"].startswith("planned"))
        self.assertGreater(tele["cost"], 0)

    def test_schema_failure_gets_one_repair_retry_then_falls_back(self):
        bad_usage = {"model": "m", "_msg": {"content": '{"nope": []}'}, "_finish": "stop"}
        with mock.patch.object(decompose, "ENABLED", True), \
             mock.patch.object(decompose, "_call_model", return_value=('{"nope": []}', bad_usage)), \
             mock.patch.object(decompose.requests, "post") as post:
            post.return_value.raise_for_status = mock.Mock()
            post.return_value.json.return_value = {
                "model": "m", "usage": {},
                "choices": [{"message": {"content": '{"nope": []}'}, "finish_reason": "stop"}]}
            subqs, tele = decompose.decompose("Some question?")
        self.assertEqual(subqs, [("Some question?", [])])
        self.assertTrue(tele["label"].startswith("fallback_schema_invalid"))
        self.assertEqual(post.call_count, 1)  # the one repair attempt


if __name__ == "__main__":
    unittest.main()
