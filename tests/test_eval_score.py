"""Unit tests for the discovery eval's scoring (pure — no SearXNG, no DB, no research run)."""
import json
import os
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("DATABASE_URL", "postgresql://unused")

from evals import run_eval, score
from pipeline import queries

SPEC = {
    "id": "t1",
    "question": "Do sellers report 3PLGuys.com holding inventory?",
    "sources": ["web_search"],
    "expect_aliases": ["3plguys"],
    "expect_terms": ["missing inventory"],
    "relevant_subreddits": ["r/FulfillmentByAmazon"],
    "relevant_hosts": ["trustpilot.com"],
    "penalty_hosts": ["3plguys.com"],
    "signal_terms": ["missing", "hold"],
}


class AnchoringTests(unittest.TestCase):
    def test_term_must_share_a_query_with_the_alias(self):
        self.assertTrue(score.anchored("3plguys missing inventory", "missing inventory",
                                       ["3plguys"]))
        self.assertFalse(score.anchored("missing inventory", "missing inventory", ["3plguys"]))

    def test_brandless_question_needs_only_the_term(self):
        self.assertTrue(score.anchored("peptide sellers reserve", "reserve", []))

    def test_split_across_queries_earns_nothing(self):
        # An alias in query 1 and a bare complaint word in query 4 retrieves every complaint on the
        # internet; scoring them independently called that a perfect plan.
        plan = ["3plguys peptides", "missing inventory"]
        self.assertEqual(score.plan_metrics(SPEC, plan)["term_coverage"], 0.0)


class PlanMetricTests(unittest.TestCase):
    def test_aim_is_full_when_the_plan_anchors(self):
        m = score.plan_metrics(SPEC, ["3plguys peptides", "3plguys missing inventory"])
        self.assertEqual(m["aim"], 1.0)

    def test_topic_only_plan_scores_zero_terms(self):
        m = score.plan_metrics(SPEC, ["3plguys peptides fulfillment"])
        self.assertEqual(m["term_coverage"], 0.0)
        self.assertEqual(m["missing_terms"], ["missing inventory"])

    def test_control_question_with_nothing_to_expect_scores_one(self):
        spec = dict(SPEC, expect_aliases=[], expect_terms=[])
        self.assertEqual(score.plan_metrics(spec, ["anything"])["aim"], 1.0)

    def test_oracle_is_not_read_from_the_generator(self):
        # Replace the failure vocabulary with nonsense: the generator changes, the labels do not,
        # so the score MUST fall. An oracle that imports its answers always reports success.
        broken = (("funds", ("zzzz",)), ("account", ("yyyy",)),
                  ("inventory", ("xxxx",)), ("reputation", ("wwww",)))
        with mock.patch.object(queries, "FAILURE_FAMILIES", broken):
            plan = queries.variants("web_search", "3plguys.com inventory")
        self.assertLess(score.plan_metrics(SPEC, plan)["aim"], 1.0)


class DiscoveryMetricTests(unittest.TestCase):
    def test_generic_subreddit_does_not_earn_reach(self):
        # This is run 28's failure. Crediting reddit.com scored it 1.0.
        m = score.discovery_metrics(SPEC, ["https://reddit.com/r/logistics/comments/1/shipping/"])
        self.assertEqual(m["reach"], 0.0)
        self.assertEqual(m["signal"], 0.0)

    def test_listed_subreddit_with_complaint_slug_earns_both(self):
        m = score.discovery_metrics(
            SPEC, ["https://reddit.com/r/FulfillmentByAmazon/comments/1/3plguys_missing_units/"])
        self.assertEqual(m["reach"], 1.0)
        self.assertEqual(m["signal"], 1.0)

    def test_vendor_page_scores_pull_not_reach(self):
        m = score.discovery_metrics(SPEC, ["https://3plguys.com/pricing"])
        self.assertEqual(m["vendor_pull"], 1.0)
        self.assertEqual(m["reach"], 0.0)

    def test_subdomain_matches_listed_host(self):
        self.assertEqual(score.discovery_metrics(SPEC, ["https://uk.trustpilot.com/review/x"]
                                                 )["reach"], 1.0)

    def test_concentration_lowers_diversity(self):
        selected = ["https://reddit.com/r/x/comments/1/a/", "https://reddit.com/r/x/comments/2/b/"]
        m = score.discovery_metrics(SPEC, selected)
        self.assertEqual(m["venues"], 1)
        self.assertEqual(m["diversity"], 0.5)
        self.assertEqual(m["thread_share"], 1.0)

    def test_empty_selection(self):
        self.assertEqual(score.discovery_metrics(SPEC, [])["reach"], 0.0)


class CompositeTests(unittest.TestCase):
    def test_vendor_pull_is_a_penalty(self):
        good = {"aim": 1.0, "reach": 1.0, "signal": 1.0, "diversity": 1.0, "vendor_pull": 0.0}
        self.assertGreater(score.composite(good), score.composite(dict(good, vendor_pull=1.0)))

    def test_signal_is_required_for_a_perfect_score(self):
        without = {"aim": 1.0, "reach": 1.0, "signal": 0.0, "diversity": 1.0, "vendor_pull": 0.0}
        self.assertLess(score.composite(without), 1.0)

    def test_clamped_to_unit_interval(self):
        self.assertEqual(score.composite({"aim": 0, "reach": 0, "signal": 0, "diversity": 0,
                                          "vendor_pull": 1.0}), 0.0)
        self.assertEqual(score.composite({"aim": 1, "reach": 1, "signal": 1, "diversity": 1,
                                          "vendor_pull": 0}), 1.0)


class ReportTests(unittest.TestCase):
    def test_plan_only_mode_reports_none_not_zero(self):
        # An offline run must never be mistaken for a run where discovery found nothing.
        row = score.score_question(SPEC, ["3plguys missing inventory"], None)
        self.assertIsNone(row["score"])
        self.assertEqual(row["mode"], "plan-only")

    def test_compare_lists_regressions_first(self):
        base = [{"id": "a", "score": 0.8, "aim": 1.0}, {"id": "b", "score": 0.5, "aim": 1.0}]
        cur = [{"id": "a", "score": 0.9, "aim": 1.0}, {"id": "b", "score": 0.2, "aim": 1.0}]
        lines = score.compare(base, cur)
        self.assertTrue(lines[0].strip().startswith("DOWN"))
        self.assertIn("b", lines[0])

    def test_compare_falls_back_to_aim_for_plan_only_baselines(self):
        # A plan-only baseline stores score=None; skipping those rows reported "no change" while
        # aim had moved from 0.65 to 1.0.
        base = [{"id": "a", "score": None, "aim": 0.5}]
        cur = [{"id": "a", "score": None, "aim": 1.0}]
        lines = score.compare(base, cur)
        self.assertEqual(len(lines), 1)
        self.assertIn("aim", lines[0])


class QuestionSetTests(unittest.TestCase):
    def test_shipped_questions_are_well_formed(self):
        specs = run_eval.load_questions()
        self.assertGreaterEqual(len(specs), 8)
        ids = set()
        for spec in specs:
            self.assertNotIn(spec["id"], ids)
            ids.add(spec["id"])
            self.assertTrue(spec["question"].strip())
            self.assertTrue(set(spec["sources"]) <= {"web_search", "reddit_threads"})
            for key in ("expect_aliases", "expect_terms", "relevant_hosts", "signal_terms"):
                self.assertIsInstance(spec[key], list, f"{spec['id']}.{key}")
            for sub in spec.get("relevant_subreddits", []):
                self.assertTrue(sub.startswith("r/"), f"{spec['id']}: {sub}")

    def test_plan_for_a_shipped_question_hits_its_expectations(self):
        # End-to-end on the real question set, still fully offline.
        for spec in run_eval.load_questions():
            plan = run_eval.plan_for(spec)
            flat = [q for qs in plan.values() for q in qs]
            m = score.plan_metrics(spec, flat)
            self.assertEqual(m["missing_aliases"], [], f"{spec['id']}: {m}")
            self.assertEqual(m["missing_terms"], [], f"{spec['id']}: {m}")

    def test_legacy_plan_scores_worse_than_the_current_plan(self):
        # The eval has to be able to SEE the change it was built to measure.
        specs = run_eval.load_questions()
        legacy = run_eval.evaluate(specs, plan_only=True, use_registry=False, legacy=True)
        current = run_eval.evaluate(specs, plan_only=True, use_registry=False)
        self.assertLess(run_eval.summarize(legacy)["mean_aim"],
                        run_eval.summarize(current)["mean_aim"])

    def test_questions_file_is_valid_json(self):
        json.loads(Path(run_eval.QUESTIONS).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
