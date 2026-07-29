"""Tests for the pooled-discovery collection path in pipeline/run.py.

run.py owns the expensive decisions — how many searches, how many pages read, how many browser
renders — and had no tests, which is how the per-sub-question budget multiplication and the
"failure queries change nothing" defect both survived review. Nothing here touches a database, a
search engine, or a browser: psycopg is stubbed at import time and every collaborator is replaced
with a recorder, so what is under test is purely the orchestration arithmetic.
"""
import os
import sys
import types
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_URL", "postgresql://unused")
os.environ.setdefault("OPENROUTER_API_KEY_ANALYST", "unused")

# pipeline.run imports psycopg at module level (it opens the run row directly). The stub only has to
# exist for import; every test replaces the collaborators it actually calls.
if "psycopg" not in sys.modules:
    stub = types.ModuleType("psycopg")
    stub.connect = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no database in tests"))
    sys.modules["psycopg"] = stub

from pipeline import run, select  # noqa: E402


def _reddit(sub, n):
    return [f"https://reddit.com/r/{sub}/comments/{sub}{i}/title/" for i in range(n)]


class BudgetTests(unittest.TestCase):
    def test_grants_are_capped_and_reported(self):
        budget = run._Budget()
        budget.threads = 5
        self.assertEqual(budget.spend("threads", 8), 5)
        self.assertEqual(budget.spend("threads", 1), 0)

    def test_refund_returns_unspent_reservation(self):
        # Reads are reserved before selection; a facet that discovers nothing must not be charged.
        budget = run._Budget()
        budget.web_reads = 12
        cap = budget.spend("web_reads", 12)
        budget.refund("web_reads", cap - 0)
        self.assertEqual(budget.web_reads, 12)

    def test_budget_is_shared_across_sub_questions(self):
        budget = run._Budget()
        budget.searches = 3
        self.assertEqual([budget.spend("searches", 1) for _ in range(5)], [1, 1, 1, 0, 0])


class WebSearchCollectionTests(unittest.TestCase):
    def setUp(self):
        self.read = []
        self.budget = run._Budget()

    def _collect(self, per_query):
        calls = []

        def discover(q, site=None, limit=10, path_must_contain=None):
            calls.append(q)
            return per_query.get(q, [])

        with mock.patch.object(run.legit, "discover_urls", side_effect=discover), \
             mock.patch.object(run.legit, "read_urls",
                               side_effect=lambda rid, urls, **k: self.read.extend(urls) or 0), \
             mock.patch.object(run.registry, "preferred", return_value=[]):
            run._collect_web_search(1, "3plguys.com peptides", "q", self.budget)
        return calls

    def test_failure_queries_are_issued_and_their_hits_are_read(self):
        # The whole point of the expansion: complaint pages a topic query never surfaces must end
        # up in the read set, not merely in a discarded candidate pool.
        base = [f"https://vendor{i}.com/marketing" for i in range(10)]
        calls = self._collect({
            "3plguys.com peptides": base,
            "3plguys reserve": ["https://complaints.com/thread"],
        })
        self.assertIn("3plguys reserve", calls)
        self.assertIn("https://complaints.com/thread", self.read)

    def test_base_tier_keeps_at_least_half_the_read_budget(self):
        # A regulatory question must keep its authoritative sources even though complaint expansion
        # is unconditional.
        base = [f"https://authority{i}.gov/doc" for i in range(10)]
        extra = [f"https://noise{i}.com/x" for i in range(10)]
        self._collect({"3plguys.com peptides": base,
                       "3plguys reserve": extra, "3plguys terminated": extra,
                       "3plguys missing inventory": extra, "3plguys \"do not use\"": extra})
        from_base = sum(1 for u in self.read if u in base)
        self.assertGreaterEqual(from_base, len(self.read) // 2)

    def test_one_domain_cannot_own_the_read_budget(self):
        loud = [f"https://loud.com/{i}" for i in range(10)]
        self._collect({"3plguys.com peptides": loud})
        self.assertLessEqual(sum(1 for u in self.read if "loud.com" in u), select.WEB_PER_DOMAIN)

    def test_empty_discovery_refunds_the_reservation(self):
        before = self.budget.web_reads
        self._collect({})
        self.assertEqual(self.budget.web_reads, before)


class RedditCollectionTests(unittest.TestCase):
    def setUp(self):
        self.budget = run._Budget()

    def _collect(self, per_query, known=()):
        sent = {}

        def discover(q, site=None, limit=10, path_must_contain=None):
            return per_query.get((site or "reddit.com", q), [])

        def request(rid, src, q, limit, urls=None):
            sent["urls"] = urls or []
            return "rid-1"

        with mock.patch.object(run.legit, "discover_urls", side_effect=discover), \
             mock.patch.object(run.reach_bridge, "request_reach", side_effect=request), \
             mock.patch.object(run.registry, "preferred", return_value=list(known)):
            result = run._collect_reddit_threads(1, "3plguys.com peptides", "q", self.budget)
        return result, sent.get("urls", [])

    def test_threads_are_spread_across_subreddits(self):
        rid, urls = self._collect({("reddit.com", "3plguys.com peptides"): _reddit("logistics", 10)})
        self.assertEqual(rid, "rid-1")
        # One subreddit's ten threads must not become ten browser renders of one opinion.
        self.assertLessEqual(len(urls), select.REDDIT_PER_SUB)

    def test_registry_subreddit_is_searched_and_prioritised(self):
        per_query = {("reddit.com", "3plguys.com peptides"): _reddit("logistics", 8),
                     ("reddit.com/r/peptides", "3plguys.com peptides"): _reddit("peptides", 3)}
        _, urls = self._collect(per_query, known=["r/peptides"])
        # Present, but NOT by displacing the base query — the registry gets a seat, not the wheel
        # (Round3FixTests covers the floor arithmetic).
        self.assertTrue(any("/r/peptides/" in u for u in urls))

    def test_no_threads_discovered_returns_none_and_refunds(self):
        before = self.budget.threads
        rid, urls = self._collect({})
        self.assertIsNone(rid)
        self.assertEqual(urls, [])
        self.assertEqual(self.budget.threads, before)

    def test_duplicate_thread_across_queries_is_rendered_once(self):
        dupe = "https://old.reddit.com/r/x/comments/1/t/"
        per_query = {("reddit.com", "3plguys.com peptides"): [dupe],
                     ("reddit.com", "3plguys reserve"): [dupe + "?utm_source=share"]}
        _, urls = self._collect(per_query)
        self.assertEqual(len(urls), 1)

    def test_run_ceiling_stops_the_third_sub_question(self):
        self.budget.threads = select.REDDIT_THREADS  # only one facet's worth left for the run
        per_query = {("reddit.com", "3plguys.com peptides"): _reddit("a", 3)}
        first, urls_a = self._collect(per_query)
        second, urls_b = self._collect(per_query)
        self.assertTrue(urls_a)
        # Second facet is bounded by what the first left behind, not given a fresh allowance.
        self.assertLessEqual(len(urls_b), select.REDDIT_THREADS - len(urls_a))



class Round3FixTests(unittest.TestCase):
    """Registry-scoped pools must be read WITHOUT taking the base query's reserved floor."""

    def _collect(self, per_query, known):
        sent = {}

        def discover(q, site=None, limit=10, path_must_contain=None):
            return per_query.get((site or "reddit.com", q), [])

        with mock.patch.object(run.legit, "discover_urls", side_effect=discover), \
             mock.patch.object(run.reach_bridge, "request_reach",
                               side_effect=lambda *a, urls=None, **k: sent.update(urls=urls or [])), \
             mock.patch.object(run.registry, "preferred", return_value=list(known)):
            run._collect_reddit_threads(1, "3plguys.com peptides", "q", run._Budget())
        return sent.get("urls", [])

    def test_two_preferred_venues_cannot_take_the_base_floor(self):
        base = _reddit("logistics", 4) + _reddit("ecommerce", 4)
        per_query = {("reddit.com", "3plguys.com peptides"): base,
                     ("reddit.com/r/peptides", "3plguys.com peptides"): _reddit("peptides", 5),
                     ("reddit.com/r/steroids", "3plguys.com peptides"): _reddit("steroids", 5)}
        urls = self._collect(per_query, known=["r/peptides", "r/steroids"])
        from_base = sum(1 for u in urls if u in base)
        self.assertGreaterEqual(from_base, len(urls) // 2)

    def test_registry_venue_is_still_read(self):
        per_query = {("reddit.com", "3plguys.com peptides"): _reddit("logistics", 10),
                     ("reddit.com/r/peptides", "3plguys.com peptides"): _reddit("peptides", 3)}
        urls = self._collect(per_query, known=["r/peptides"])
        self.assertTrue(any("/r/peptides/" in u for u in urls))


class ThrottleVisibilityTests(unittest.TestCase):
    """A rate-limited search returns 200 with an empty result list, exactly like a search that found
    nothing. Conflating them is how "the engines suspended us" becomes "no evidence exists"."""

    def setUp(self):
        from collectors import search
        self.search = search
        search.throttled_queries = 0
        self.calls = []

    def _fake_query(self, results, unresponsive):
        def q(query, pageno=1):
            self.calls.append(query)
            return results, unresponsive
        return q

    def test_empty_with_unresponsive_engines_is_counted_as_throttled(self):
        with mock.patch.object(self.search, "searxng_query",
                               side_effect=self._fake_query([], [["google cse", "too many"]])), \
             mock.patch.object(self.search.time, "sleep"):
            self.assertEqual(self.search.discover_urls("x"), [])
        self.assertEqual(self.search.throttled_queries, 1)
        self.assertEqual(len(self.calls), 2, "should retry once after backoff")

    def test_genuinely_empty_topic_is_not_counted_as_throttled(self):
        with mock.patch.object(self.search, "searxng_query", side_effect=self._fake_query([], [])):
            self.assertEqual(self.search.discover_urls("x"), [])
        self.assertEqual(self.search.throttled_queries, 0)
        self.assertEqual(len(self.calls), 1, "no retry when nothing was suspended")

    def test_retry_that_succeeds_is_not_counted(self):
        results = [{"url": "https://a.com/1"}]
        seq = [([], [["ddg", "403"]]), (results, [])]
        with mock.patch.object(self.search, "searxng_query",
                               side_effect=lambda *a, **k: seq.pop(0)), \
             mock.patch.object(self.search.time, "sleep"):
            self.assertEqual(self.search.discover_urls("x"), ["https://a.com/1"])
        self.assertEqual(self.search.throttled_queries, 0)

    def test_engine_name_formatting_survives_shape_changes(self):
        self.assertEqual(self.search._names([["google cse", "too many"], "ddg"]), "google cse, ddg")
        self.assertEqual(self.search._names([]), "unknown")


class Round4FixTests(unittest.TestCase):
    """Second-review findings on the throttle path: partial degradation, counter safety, budgets."""

    def setUp(self):
        from collectors import search
        self.search = search
        search.throttled_queries = 0
        search.degraded_queries = 0
        search._retries_used = 0

    def test_partial_results_are_recorded_as_degraded_not_clean(self):
        # Results came back, but some engines were suspended — the set is missing their share, and
        # a baseline frozen here would be measuring an outage.
        with mock.patch.object(self.search, "searxng_query",
                               return_value=([{"url": "https://a.com/1"}], [["ddg", "403"]])):
            self.assertEqual(self.search.discover_urls("x"), ["https://a.com/1"])
        self.assertEqual(self.search.degraded_queries, 1)
        self.assertEqual(self.search.throttled_queries, 0)

    def test_throttle_is_counted_even_if_the_retry_raises(self):
        # Counting after the retry meant an exception in the retry erased the evidence that the
        # first attempt was throttled at all.
        calls = {"n": 0}

        def flaky(query, pageno=1):
            calls["n"] += 1
            if calls["n"] == 1:
                return [], [["google cse", "too many"]]
            raise RuntimeError("connection reset")

        with mock.patch.object(self.search, "searxng_query", side_effect=flaky), \
             mock.patch.object(self.search.time, "sleep"):
            self.assertEqual(self.search.discover_urls("x"), [])
        self.assertEqual(self.search.throttled_queries, 1)

    def test_successful_retry_is_not_left_counted_as_throttled(self):
        seq = [([], [["ddg", "403"]]), ([{"url": "https://a.com/1"}], [])]
        with mock.patch.object(self.search, "searxng_query", side_effect=lambda *a, **k: seq.pop(0)), \
             mock.patch.object(self.search.time, "sleep"):
            self.assertEqual(self.search.discover_urls("x"), ["https://a.com/1"])
        self.assertEqual(self.search.throttled_queries, 0)

    def test_retry_budget_is_bounded(self):
        self.search._retries_used = self.search.MAX_RETRIES
        with mock.patch.object(self.search, "searxng_query",
                               return_value=([], [["ddg", "403"]])) as q, \
             mock.patch.object(self.search.time, "sleep"):
            self.search.discover_urls("x")
        self.assertEqual(q.call_count, 1, "no retry once the process budget is spent")
        self.assertEqual(self.search.throttled_queries, 1)

    def test_pacing_is_shared_across_processes(self):
        # A per-process gap does nothing about the aggregate rate the engines actually see, which
        # is what gets the box suspended when two runs execute in parallel.
        import time as _t
        self.search.PACE_FILE = os.path.join(
            os.environ.get("TEMP", "/tmp"), "hermes-pace-test")
        try:
            os.unlink(self.search.PACE_FILE)
        except OSError:
            pass
        original = self.search.PACING_SECONDS
        self.search.PACING_SECONDS = 0.4
        try:
            self.search._pace()
            start = _t.time()
            self.search._pace()          # a second caller must wait out the remainder
            self.assertGreater(_t.time() - start, 0.2)
        finally:
            self.search.PACING_SECONDS = original


class ExtractionFallbackTests(unittest.TestCase):
    """Paid fallback engages only after the free model refuses (owner directive 2026-07-26)."""

    def setUp(self):
        from pipeline import extract
        self.extract = extract
        extract._fallback_active = False

    def tearDown(self):
        self.extract._fallback_active = False

    def test_starts_on_the_free_model(self):
        self.assertFalse(self.extract._use_fallback())
        self.assertEqual(self.extract.EXTRACT_MODEL, "nvidia/nemotron-3-ultra-550b-a55b:free")

    @staticmethod
    def _budget(blocked: bool, why: str = ""):
        """Stub collectors.common for the lazy import inside _latch_fallback."""
        stub = types.SimpleNamespace(over_budget=lambda run_id, **kw: (blocked, why))
        return mock.patch.dict(sys.modules,
                               {"collectors": types.SimpleNamespace(common=stub),
                                "collectors.common": stub})

    def test_daily_exhaustion_latches_the_paid_model(self):
        with self._budget(False):
            self.assertTrue(self.extract._latch_fallback("free-models-per-day-high-balance", 1))
        self.assertTrue(self.extract._use_fallback())

    def test_latch_is_idempotent_and_sticky(self):
        with self._budget(False):
            self.extract._latch_fallback("first", 1)
            self.extract._latch_fallback("second", 1)
        self.assertTrue(self.extract._use_fallback())

    def test_fallback_can_be_disabled(self):
        with mock.patch.object(self.extract, "FALLBACK_ENABLED", False):
            self.assertFalse(self.extract._latch_fallback("per-day", 1))
            self.assertFalse(self.extract._use_fallback())

    def test_over_budget_run_does_not_latch_the_paid_model(self):
        """Regression for the defect that made 2026-07-28 cost real money.

        The guard used to sit in extract_run() as `_fallback_active = False` — already the
        default, so it disabled nothing — while _latch_fallback consulted only FALLBACK_ENABLED.
        The first worker thread to see a free-tier 429 therefore switched the whole run onto the
        paid model no matter what the cap said, which is the opposite of what its docstring
        promised. Fourteen concurrent runs did exactly that.
        """
        with self._budget(True, "daily cap $2.00 reached (today, all runs: $2.4100)"):
            self.assertFalse(
                self.extract._latch_fallback("free-models-per-day-high-balance", 1))
        self.assertFalse(self.extract._use_fallback())

    def test_fallback_slug_is_pinned_exactly(self):
        # The stack pins exact slugs, never aliases — an alias drifts to whatever is newest.
        self.assertEqual(self.extract.FALLBACK_MODEL, "deepseek/deepseek-v4-flash")
        self.assertNotIn(":free", self.extract.FALLBACK_MODEL)

if __name__ == "__main__":
    unittest.main()
