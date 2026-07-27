"""Unit tests for diversity-aware candidate selection (pure, no network/DB)."""
import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://unused")

from pipeline import select


class KeyTests(unittest.TestCase):
    def test_domain_key_collapses_subdomains(self):
        # blog./support./docs. are one publisher; separate allowances let a vendor take the budget.
        self.assertEqual(select.domain_key("https://support.Example.com/a"), "example.com")
        self.assertEqual(select.domain_key("https://www.example.com/a"), "example.com")

    def test_domain_key_handles_multi_label_tlds(self):
        self.assertEqual(select.domain_key("https://shop.vendor.co.uk/x"), "vendor.co.uk")

    def test_reddit_key_is_the_subreddit(self):
        # Keying Reddit on host would collapse every candidate into one group and defeat the point.
        self.assertEqual(
            select.reddit_key("https://old.reddit.com/r/Logistics/comments/abc/title/"),
            "r/logistics")

    def test_reddit_url_without_subreddit_falls_back_to_host(self):
        self.assertEqual(select.reddit_key("https://reddit.com/user/someone"), "reddit.com")

    def test_non_reddit_url_uses_domain(self):
        self.assertEqual(select.reddit_key("https://trustpilot.com/review/x"), "trustpilot.com")

    def test_unparseable_url(self):
        self.assertEqual(select.domain_key("not a url"), "")


class CanonicalTests(unittest.TestCase):
    def test_reddit_host_aliases_are_the_same_thread(self):
        a = select.canonical("https://old.reddit.com/r/x/comments/1/t/")
        b = select.canonical("https://www.reddit.com/r/x/comments/1/t/?utm_source=share")
        self.assertEqual(a, b)

    def test_tracking_params_stripped_real_params_kept(self):
        self.assertEqual(select.canonical("https://a.com/x?utm_source=q&page=2"),
                         "https://a.com/x?page=2")

    def test_trailing_slash_and_fragment(self):
        self.assertEqual(select.canonical("https://a.com/x/#c"), select.canonical("https://a.com/x"))


class DedupeTests(unittest.TestCase):
    def test_duplicate_thread_is_not_rendered_twice(self):
        out = select.dedupe(["https://old.reddit.com/r/x/comments/1/t/",
                             "https://www.reddit.com/r/x/comments/1/t/?utm_source=share"])
        self.assertEqual(len(out), 1)

    def test_order_is_preserved_because_it_is_search_rank(self):
        out = select.dedupe(["https://b.com/1", "https://a.com/1", "https://b.com/1"])
        self.assertEqual(out, ["https://b.com/1", "https://a.com/1"])

    def test_junk_is_dropped(self):
        self.assertEqual(select.dedupe(["", None, "https://a.com"]), ["https://a.com"])


class InterleaveTests(unittest.TestCase):
    def test_best_of_each_pool_comes_first(self):
        # Concatenation would put every hit of the base query ahead of every failure-query hit,
        # so with a tight cap the extra searches would cost time and change nothing.
        out = select.interleave([["a1", "a2", "a3"], ["b1"], ["c1", "c2"]])
        self.assertEqual(out, ["a1", "b1", "c1", "a2", "c2", "a3"])

    def test_empty_pools_ignored(self):
        self.assertEqual(select.interleave([[], ["b1"], []]), ["b1"])
        self.assertEqual(select.interleave([]), [])


class SelectDiverseTests(unittest.TestCase):
    def setUp(self):
        # One host dominates the ranking — exactly what a search engine returns.
        self.candidates = [
            "https://loud.com/1", "https://loud.com/2", "https://loud.com/3",
            "https://loud.com/4", "https://quiet.com/1", "https://other.com/1",
        ]

    def test_round_robin_spreads_before_repeating(self):
        out = select.select_diverse(self.candidates, total_cap=4, per_key_cap=2)
        self.assertEqual(out, ["https://loud.com/1", "https://quiet.com/1",
                               "https://other.com/1", "https://loud.com/2"])

    def test_per_key_cap_is_enforced(self):
        out = select.select_diverse(self.candidates, total_cap=10, per_key_cap=1)
        self.assertEqual(len(out), 3)
        self.assertEqual(len({select.domain_key(u) for u in out}), 3)

    def test_total_cap_is_enforced(self):
        self.assertEqual(len(select.select_diverse(self.candidates, total_cap=2, per_key_cap=5)), 2)

    def test_priority_venue_goes_first(self):
        out = select.select_diverse(self.candidates, total_cap=2, per_key_cap=1,
                                    priority=["QUIET.com"])
        self.assertEqual(out[0], "https://quiet.com/1")

    def test_priority_keeps_the_registrys_own_ranking(self):
        # rank_rows already ordered these; converting to a set discarded that order and let search
        # rank decide which preferred venue won the single slot.
        out = select.select_diverse(self.candidates, total_cap=1, per_key_cap=1,
                                    priority=["other.com", "quiet.com"])
        self.assertEqual(out, ["https://other.com/1"])

    def test_priority_cannot_invent_a_venue(self):
        out = select.select_diverse(self.candidates, total_cap=6, per_key_cap=3,
                                    priority=["never-discovered.com"])
        self.assertNotIn("never-discovered.com", {select.domain_key(u) for u in out})

    def test_subreddit_diversity(self):
        urls = [f"https://reddit.com/r/logistics/comments/{i}/t/" for i in range(5)]
        urls += ["https://reddit.com/r/peptides/comments/9/t/"]
        out = select.select_diverse(urls, total_cap=3, per_key_cap=2, key=select.reddit_key)
        self.assertEqual(out[1], "https://reddit.com/r/peptides/comments/9/t/")

    def test_degenerate_inputs(self):
        self.assertEqual(select.select_diverse([], total_cap=5, per_key_cap=2), [])
        self.assertEqual(select.select_diverse(self.candidates, total_cap=0, per_key_cap=2), [])
        self.assertEqual(select.select_diverse(self.candidates, total_cap=5, per_key_cap=0), [])

    def test_exhausted_candidates_do_not_loop_forever(self):
        out = select.select_diverse(["https://a.com/1"], total_cap=50, per_key_cap=50)
        self.assertEqual(out, ["https://a.com/1"])

    def test_deterministic(self):
        first = select.select_diverse(self.candidates, total_cap=4, per_key_cap=2)
        second = select.select_diverse(self.candidates, total_cap=4, per_key_cap=2)
        self.assertEqual(first, second)


class SelectTieredTests(unittest.TestCase):
    def setUp(self):
        self.base = [f"https://base{i}.com/x" for i in range(6)]
        self.extra = [f"https://extra{i}.com/x" for i in range(6)]

    def test_base_tier_floor_is_reserved(self):
        # The regulatory-control guarantee: complaint expansion cannot take the whole read budget.
        out = select.select_tiered([self.base, self.extra], total_cap=6, per_key_cap=1, floors=[3])
        from_base = sum(1 for u in out if u in self.base)
        self.assertGreaterEqual(from_base, 3)
        self.assertEqual(len(out), 6)

    def test_remaining_slots_are_shared(self):
        out = select.select_tiered([self.base, self.extra], total_cap=6, per_key_cap=1, floors=[2])
        self.assertTrue(any(u in self.extra for u in out))

    def test_venue_cap_is_global_across_tiers(self):
        # A venue in both tiers must not draw its full allowance twice.
        shared = ["https://same.com/1", "https://same.com/2", "https://same.com/3"]
        out = select.select_tiered([shared, shared], total_cap=6, per_key_cap=2)
        self.assertEqual(len(out), 2)

    def test_no_duplicate_urls_across_tiers(self):
        out = select.select_tiered([self.base, self.base], total_cap=6, per_key_cap=2)
        self.assertEqual(len(out), len(set(out)))

    def test_floor_larger_than_cap_is_clamped(self):
        out = select.select_tiered([self.base, self.extra], total_cap=2, per_key_cap=1, floors=[99])
        self.assertEqual(len(out), 2)

    def test_empty_base_tier_still_uses_expansion(self):
        out = select.select_tiered([[], self.extra], total_cap=3, per_key_cap=1, floors=[2])
        self.assertEqual(len(out), 3)


class EnvTests(unittest.TestCase):
    def test_malformed_env_does_not_raise(self):
        os.environ["HERMES_TEST_KNOB"] = "not-a-number"
        try:
            self.assertEqual(select.int_env("HERMES_TEST_KNOB", 7), 7)
        finally:
            del os.environ["HERMES_TEST_KNOB"]



class Round2FixTests(unittest.TestCase):
    """Defects found in the second adversarial review pass."""

    def test_unlisted_three_label_hosts_are_not_collapsed_together(self):
        # A hard-coded suffix list turned a.co.in and b.co.in both into "co.in", so four
        # independent publishers shared one venue's allowance.
        self.assertEqual(select.domain_key("https://a.co.in/x"), "a.co.in")
        self.assertNotEqual(select.domain_key("https://a.co.in/x"),
                            select.domain_key("https://b.co.in/x"))

    def test_known_and_unknown_multi_label_suffixes_both_work(self):
        self.assertEqual(select.domain_key("https://shop.vendor.co.uk/x"), "vendor.co.uk")
        self.assertEqual(select.domain_key("https://shop.vendor.com.tr/x"), "vendor.com.tr")

    def test_lookalike_host_is_not_treated_as_reddit(self):
        # notreddit.com ends with "reddit.com"; a suffix test let it into Reddit's namespace, where
        # it could displace a real thread during dedup.
        self.assertFalse(select.is_reddit("notreddit.com"))
        self.assertTrue(select.is_reddit("old.reddit.com"))
        self.assertNotEqual(select.canonical("https://notreddit.com/r/x/comments/1/t/"),
                            select.canonical("https://reddit.com/r/x/comments/1/t/"))

    def test_malformed_float_knob_does_not_raise_at_import(self):
        for bad in ("oops", "nan", "inf", "-0.5"):
            os.environ["HERMES_TEST_FLOAT"] = bad
            try:
                self.assertEqual(select.float_env("HERMES_TEST_FLOAT", 0.5, hi=1.0), 0.5, bad)
            finally:
                del os.environ["HERMES_TEST_FLOAT"]

    def test_duration_knobs_are_not_clamped_to_a_fraction(self):
        # float_env serves both fractions (BASE_TIER_FLOOR) and durations (search pacing). A
        # hard-coded 0-1 range made every duration override snap silently back to its default.
        os.environ["HERMES_TEST_FLOAT"] = "8.0"
        try:
            self.assertEqual(select.float_env("HERMES_TEST_FLOAT", 1.5), 8.0)
            self.assertEqual(select.float_env("HERMES_TEST_FLOAT", 0.5, hi=1.0), 0.5)
        finally:
            del os.environ["HERMES_TEST_FLOAT"]
        os.environ["HERMES_TEST_FLOAT"] = "0.25"
        try:
            self.assertEqual(select.float_env("HERMES_TEST_FLOAT", 0.5), 0.25)
        finally:
            del os.environ["HERMES_TEST_FLOAT"]

if __name__ == "__main__":
    unittest.main()
