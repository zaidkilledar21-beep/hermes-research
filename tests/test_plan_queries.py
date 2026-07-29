"""Tests for the v3 query planner (pipeline/plan_queries.py) — pure, no DB, no network.

The regression that matters most here is the fail-soft contract: with no plan, or for any source
outside PLANNABLE_SOURCES, to_variants() must be BYTE-IDENTICAL to queries.variants(). The planner
is an enhancement layered over a working deterministic path; these tests are what keeps a planner
bug from ever costing more than the plan it failed to produce.
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

from pipeline import plan_queries, queries  # noqa: E402


def _msg(content, finish="stop"):
    return {"content": content}, finish


GOOD_PLAN = {
    "anchors": ["3plguys", "3PLGuys"],
    "vocabulary": ["cold chain", "lyophilized"],
    "queries": [
        {"q": "3plguys reserve frozen", "intent": "complaint",
         "expected_evidence": "an operator reporting withheld funds"},
        {"q": "3plguys cold chain pricing", "intent": "price",
         "expected_evidence": "a quote or invoice discussion"},
    ],
}


class ClassifyTests(unittest.TestCase):
    def test_clean_json_ok(self):
        plan, state, _ = plan_queries.classify_plan_response(*_msg(json.dumps(GOOD_PLAN)))
        self.assertEqual(state, "ok")
        self.assertEqual(len(plan["queries"]), 2)

    def test_fenced_json_ok(self):
        raw = "```json\n" + json.dumps(GOOD_PLAN) + "\n```"
        plan, state, _ = plan_queries.classify_plan_response(*_msg(raw))
        self.assertEqual(state, "ok")

    def test_prose_wrapped_object_ok(self):
        raw = "Here is the plan: " + json.dumps(GOOD_PLAN) + " Hope that helps."
        plan, state, _ = plan_queries.classify_plan_response(*_msg(raw))
        self.assertEqual(state, "ok")

    def test_missing_queries_key_is_schema_invalid(self):
        plan, state, _ = plan_queries.classify_plan_response(*_msg('{"anchors": ["x"]}'))
        self.assertIsNone(plan)
        self.assertEqual(state, "schema_invalid")

    def test_truncated_finish_reason(self):
        plan, state, _ = plan_queries.classify_plan_response(
            {"content": '{"queries": ['}, "length")
        self.assertIsNone(plan)
        self.assertEqual(state, "truncated")

    def test_empty_content_is_parse_failed(self):
        plan, state, _ = plan_queries.classify_plan_response({"content": ""}, "stop")
        self.assertEqual(state, "parse_failed")

    def test_reasoning_field_used_when_content_empty(self):
        plan, state, _ = plan_queries.classify_plan_response(
            {"content": "", "reasoning": json.dumps(GOOD_PLAN)}, "stop")
        self.assertEqual(state, "ok")


class ValidateTests(unittest.TestCase):
    def test_caps_enforced(self):
        big = {"anchors": [f"a{i}" for i in range(10)],
               "vocabulary": [f"v{i}" for i in range(10)],
               "queries": [{"q": f"q {i}", "intent": "other", "expected_evidence": "d"}
                           for i in range(10)]}
        plan = plan_queries._validate_plan(big)
        self.assertEqual(len(plan["anchors"]), plan_queries.MAX_ANCHORS)
        self.assertEqual(len(plan["vocabulary"]), plan_queries.MAX_VOCAB)
        self.assertEqual(len(plan["queries"]), plan_queries.MAX_QUERIES)

    def test_invalid_intent_normalized_to_other(self):
        plan = plan_queries._validate_plan(
            {"queries": [{"q": "x y", "intent": "banana", "expected_evidence": ""}]})
        self.assertEqual(plan["queries"][0]["intent"], "other")

    def test_bad_entries_dropped_not_fatal(self):
        plan = plan_queries._validate_plan(
            {"queries": ["not a dict", {"q": ""}, {"q": "good query", "intent": "price"}]})
        self.assertEqual(len(plan["queries"]), 1)
        self.assertEqual(plan["queries"][0]["q"], "good query")

    def test_all_invalid_returns_none(self):
        self.assertIsNone(plan_queries._validate_plan({"queries": ["nope", {"q": ""}]}))
        self.assertIsNone(plan_queries._validate_plan({"queries": "not a list"}))
        self.assertIsNone(plan_queries._validate_plan("not a dict"))


class ToVariantsTests(unittest.TestCase):
    """The fail-soft contract — the most important tests in this file."""

    def test_no_plan_is_byte_identical_for_every_source(self):
        base = "3PLGuys peptide fulfillment reserve frozen"
        for src in ["web_search", "reddit_threads", "x", "github", "hackernews",
                    "reddit_reach", "stackexchange_reach", "web", "trustpilot_reach"]:
            self.assertEqual(plan_queries.to_variants(None, src, base),
                             queries.variants(src, base), f"source {src} diverged with plan=None")

    def test_plan_ignored_for_non_plannable_source(self):
        base = "peptide suppliers"
        for src in ["x", "github", "hackernews", "reddit_reach", "web"]:
            self.assertEqual(plan_queries.to_variants(GOOD_PLAN, src, base),
                             queries.variants(src, base), f"source {src} consumed a plan")

    def test_plan_used_for_web_search_base_first(self):
        out = plan_queries.to_variants(GOOD_PLAN, "web_search", "base topic query")
        self.assertEqual(out[0], "base topic query")   # BASE_TIER_FLOOR depends on this
        self.assertIn("3plguys reserve frozen", out)

    def test_plan_is_superset_of_deterministic(self):
        # The measured lesson: replacing the failure families cost aim (1.00 -> 0.875).
        # A plan may only ADD queries — every deterministic query must survive.
        base = "3PLGuys peptide fulfillment reserve frozen"
        out = plan_queries.to_variants(GOOD_PLAN, "web_search", base)
        for q in queries.variants("web_search", base):
            self.assertIn(q, out, f"deterministic query lost: {q}")

    def test_cap_matches_superset_contract(self):
        many = {"queries": [{"q": f"query {i}", "intent": "other", "expected_evidence": ""}
                            for i in range(20)]}
        out = plan_queries.to_variants(plan_queries._validate_plan(many), "web_search", "base")
        det = len(queries.variants("web_search", "base"))
        self.assertLessEqual(len(out), det + plan_queries.PLANNER_EXTRA)

    def test_dedup_against_base(self):
        dup = {"queries": [{"q": "base", "intent": "other", "expected_evidence": ""},
                           {"q": "other query", "intent": "other", "expected_evidence": ""}]}
        out = plan_queries.to_variants(plan_queries._validate_plan(dup), "web_search", "base")
        self.assertEqual(out[0], "base")
        self.assertEqual(out.count("base"), 1)
        self.assertIn("other query", out)


class ShortQueryTests(unittest.TestCase):
    def test_prefers_short_plan_query(self):
        self.assertEqual(plan_queries.short_query(GOOD_PLAN, "a b c d e f g h"),
                         "3plguys reserve frozen")

    def test_falls_back_to_truncated_base(self):
        self.assertEqual(plan_queries.short_query(None, "a b c d e f g h"), "a b c d")

    def test_anchor_vocab_fallback_when_all_plan_queries_long(self):
        plan = {"anchors": ["vendorx"], "vocabulary": ["reserve", "chargeback"],
                "queries": [{"q": "one two three four five six", "intent": "other",
                             "expected_evidence": ""}]}
        out = plan_queries.short_query(plan, "base words here")
        self.assertTrue(out.startswith("vendorx"))
        self.assertLessEqual(len(out.split()), 4)


class PlanSubQuestionTests(unittest.TestCase):
    """Transport/parse retry discipline with requests.post mocked."""

    def _response(self, content, finish="stop", model="tencent/hy3-20260706"):
        r = mock.Mock()
        r.raise_for_status = mock.Mock()
        r.json.return_value = {
            "model": model, "usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": 3e-05},
            "choices": [{"message": {"content": content}, "finish_reason": finish}]}
        return r

    def test_success_first_try(self):
        with mock.patch.object(plan_queries.requests, "post",
                               return_value=self._response(json.dumps(GOOD_PLAN))) as post:
            plan, tele = plan_queries.plan_sub_question("Q", "SQ", ["web_search"])
        self.assertEqual(tele["state"], "planned")
        self.assertEqual(len(plan["queries"]), 2)
        self.assertEqual(post.call_count, 1)
        self.assertGreater(tele["cost"], 0)

    def test_transport_failure_retries_once_then_falls_back(self):
        with mock.patch.object(plan_queries.requests, "post",
                               side_effect=OSError("boom")) as post, \
             mock.patch.object(plan_queries.time, "sleep"):
            plan, tele = plan_queries.plan_sub_question("Q", "SQ", ["web_search"])
        self.assertIsNone(plan)
        self.assertEqual(tele["state"], "degraded_ok_transport_fallback")
        self.assertEqual(post.call_count, 2)

    def test_schema_failure_gets_repair_retry_then_falls_back(self):
        bad = self._response('{"anchors": []}')  # no queries key
        with mock.patch.object(plan_queries.requests, "post", return_value=bad) as post:
            plan, tele = plan_queries.plan_sub_question("Q", "SQ", ["web_search"])
        self.assertIsNone(plan)
        self.assertEqual(tele["state"], "fallback_schema_invalid")
        self.assertEqual(post.call_count, 2)  # original + one bounded repair

    def test_repair_retry_can_succeed(self):
        bad = self._response("total garbage not json")
        good = self._response(json.dumps(GOOD_PLAN))
        with mock.patch.object(plan_queries.requests, "post", side_effect=[bad, good]):
            plan, tele = plan_queries.plan_sub_question("Q", "SQ", ["web_search"])
        self.assertEqual(tele["state"], "planned")
        self.assertIsNotNone(plan)

    def test_truncation_does_not_retry(self):
        trunc = self._response('{"queries": [', finish="length")
        with mock.patch.object(plan_queries.requests, "post", return_value=trunc) as post:
            plan, tele = plan_queries.plan_sub_question("Q", "SQ", ["web_search"])
        self.assertIsNone(plan)
        self.assertEqual(tele["state"], "fallback_truncated")
        self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
