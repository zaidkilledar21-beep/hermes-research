"""Unit tests for the vertical source registry's pure logic (no DB is touched)."""
import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://unused")

from pipeline import registry


class ClassifyUrlTests(unittest.TestCase):
    def test_reddit_thread_is_a_subreddit_venue(self):
        self.assertEqual(
            registry.classify_url("https://old.reddit.com/r/Peptides/comments/abc/title/"),
            ("subreddit", "r/peptides"))

    def test_reddit_without_a_subreddit_is_not_a_venue(self):
        self.assertIsNone(registry.classify_url("https://www.reddit.com/"))

    def test_site_venue_strips_www(self):
        self.assertEqual(registry.classify_url("https://www.Trustpilot.com/review/x"),
                         ("site", "trustpilot.com"))

    def test_ignored_hosts_and_junk(self):
        for url in ("https://google.com/search?q=x", "https://t.co/abc", "", None, "not a url"):
            self.assertIsNone(registry.classify_url(url), url)


class TopicTests(unittest.TestCase):
    def test_vendor_survives_truncation(self):
        # Sorting BEFORE truncating dropped the vendor whenever it sorted late, which is how two
        # questions about different vendors ended up sharing a topic.
        tokens = registry.topic_tokens(
            "What do sellers say about zzzvendor.com withholding weekly settlement payouts", limit=3)
        self.assertIn("zzzvendor.com", tokens)

    def test_fingerprint_is_sorted_lowercased_and_deduped(self):
        tokens = registry.topic_tokens("Does 3PLGuys freeze funds? 3PLGuys reserve", limit=5)
        self.assertEqual(tokens, sorted(tokens))
        self.assertEqual(len(tokens), len(set(tokens)))
        self.assertTrue(all(t == t.lower() for t in tokens))

    def test_word_order_does_not_change_the_topic(self):
        a = registry.topic_tokens("peptide 3PL inventory problems")
        b = registry.topic_tokens("inventory problems peptide 3PL")
        self.assertEqual(a, b)

    def test_topic_key_matches_tokens(self):
        q = "Do peptide sellers report 3PLGuys holding inventory"
        self.assertEqual(registry.topic_key(q), "-".join(registry.topic_tokens(q)))

    def test_empty_question(self):
        self.assertEqual(registry.topic_tokens(""), [])
        self.assertEqual(registry.topic_key(""), "")


class AggregateTests(unittest.TestCase):
    def test_counts_totals_and_useful_separately(self):
        rows = [("https://reddit.com/r/x/comments/1/a/", True, "community"),
                ("https://reddit.com/r/x/comments/2/b/", False, "community"),
                ("https://reddit.com/r/x/comments/3/c/", True, "community"),
                ("https://trustpilot.com/review/y", True, "independent_review")]
        self.assertEqual(registry.aggregate_run(rows), {
            ("subreddit", "r/x"): (3, 2), ("site", "trustpilot.com"): (1, 1)})

    def test_vendor_marketing_earns_no_credit(self):
        # Otherwise the vendor's own site promotes itself into permanent priority.
        rows = [("https://vendor.com/pricing", True, "vendor_marketing")]
        self.assertEqual(registry.aggregate_run(rows), {("site", "vendor.com"): (1, 0)})

    def test_null_relevance_counts_as_retrieved_but_never_useful(self):
        # An extraction outage must not read as a venue endorsement.
        rows = [("https://a.com/1", None, "community"), ("https://a.com/2", None, "community")]
        self.assertEqual(registry.aggregate_run(rows), {("site", "a.com"): (2, 0)})

    def test_missing_tier_column_is_tolerated(self):
        self.assertEqual(registry.aggregate_run([("https://a.com/1", True)]),
                         {("site", "a.com"): (1, 1)})

    def test_venueless_urls_are_skipped(self):
        self.assertEqual(registry.aggregate_run([("https://google.com/search", True, None)]), {})


class RankTests(unittest.TestCase):
    """rows are (identifier, useful_runs, useful_hits, total_items, topic_tokens)."""

    def test_topic_overlap_beats_raw_hits(self):
        target = ["peptides", "3pl", "inventory"]
        rows = [("r/generic", 9, 50, 500, ["peptides", "3pl", "logistics"]),   # overlap 2
                ("r/niche", 9, 4, 5, ["peptides", "3pl", "inventory"])]        # overlap 3
        self.assertEqual([i for i, _ in registry.rank_rows(rows, target)],
                         ["r/niche", "r/generic"])

    def test_unrelated_topic_is_filtered_out_entirely(self):
        rows = [("r/anime", 9, 90, 100, ["anime", "manga"])]
        self.assertEqual(registry.rank_rows(rows, ["peptides", "3pl"]), [])

    def test_distinct_runs_outrank_item_volume(self):
        # Two replies in ONE thread of ONE run must not beat a venue proven across four runs.
        rows = [("r/loud", 1, 40, 200, ["peptides", "3pl"]),
                ("r/steady", 4, 8, 20, ["peptides", "3pl"])]
        self.assertEqual([i for i, _ in registry.rank_rows(rows, ["peptides", "3pl"])],
                         ["r/steady", "r/loud"])

    def test_hit_rate_beats_volume_at_equal_runs(self):
        rows = [("r/loud", 3, 6, 300, ["peptides", "3pl"]),
                ("r/small", 3, 4, 5, ["peptides", "3pl"])]
        self.assertEqual([i for i, _ in registry.rank_rows(rows, ["peptides", "3pl"])],
                         ["r/small", "r/loud"])

    def test_single_shared_token_is_not_the_same_subject(self):
        rows = [("r/payments", 5, 20, 25, ["peptides", "chargeback"])]
        self.assertEqual(registry.rank_rows(rows, ["peptides", "3pl", "inventory"]), [])

    def test_rows_for_one_venue_are_merged_not_duplicated(self):
        # (kind, identifier, topic_key) is unique, so one subreddit can hold several rows — emitting
        # it twice made the caller run the identical site-scoped search twice.
        rows = [("r/x", 2, 4, 10, ["peptides", "3pl"]),
                ("r/x", 3, 6, 12, ["peptides", "3pl", "inventory"])]
        ranked = registry.rank_rows(rows, ["peptides", "3pl"])
        self.assertEqual([i for i, _ in ranked], ["r/x"])

    def test_stable_tiebreak_on_identifier(self):
        rows = [("b.com", 2, 2, 4, ["x", "y"]), ("a.com", 2, 2, 4, ["x", "y"])]
        self.assertEqual([i for i, _ in registry.rank_rows(rows, ["x", "y"])], ["a.com", "b.com"])

    def test_zero_total_does_not_divide_by_zero(self):
        self.assertEqual(registry.rank_rows([("a.com", 1, 0, 0, ["x", "y"])], ["x", "y"]),
                         [("a.com", 2.0)])

    def test_narrow_topic_falls_back_to_available_overlap(self):
        # A one-token topic cannot meet a two-token floor; requiring it would disable the registry.
        rows = [("a.com", 2, 4, 8, ["peptides"])]
        self.assertEqual([i for i, _ in registry.rank_rows(rows, ["peptides"])], ["a.com"])

    def test_empty_rows(self):
        self.assertEqual(registry.rank_rows([], ["x"]), [])



class Round2FixTests(unittest.TestCase):
    """Defects found in the second adversarial review pass."""

    def test_vendor_owned_community_page_earns_no_credit(self):
        # A vendor-run subreddit still carries credibility_tier='community', so the tier filter
        # alone cannot see it — ownership has to be checked separately.
        rows = [("https://reddit.com/r/vendorofficial/comments/1/a/", True, "community",
                 "vendor_owned")]
        self.assertEqual(registry.aggregate_run(rows), {("subreddit", "r/vendorofficial"): (1, 0)})

    def test_affiliate_and_sponsored_pages_earn_no_credit(self):
        for ownership in ("affiliate_leadgen", "sponsored"):
            rows = [("https://blog.com/x", True, "general_web", ownership)]
            self.assertEqual(registry.aggregate_run(rows), {("site", "blog.com"): (1, 0)},
                             ownership)

    def test_independent_community_page_still_earns_credit(self):
        rows = [("https://reddit.com/r/peptides/comments/1/a/", True, "community", "community")]
        self.assertEqual(registry.aggregate_run(rows), {("subreddit", "r/peptides"): (1, 1)})

    def test_site_venue_uses_the_same_identity_selection_groups_by(self):
        # Registry rows stored the exact host, selection groups by registrable domain, so a
        # priority of "docs.vendor.com" never matched the "vendor.com" candidate bucket.
        from pipeline import select

        kind, identifier = registry.classify_url("https://docs.vendor.com/help")
        self.assertEqual((kind, identifier), ("site", "vendor.com"))
        self.assertEqual(identifier, select.domain_key("https://blog.vendor.com/post"))

    def test_lookalike_reddit_host_is_a_plain_site(self):
        self.assertEqual(registry.classify_url("https://notreddit.com/r/x/comments/1/t/"),
                         ("site", "notreddit.com"))

if __name__ == "__main__":
    unittest.main()
