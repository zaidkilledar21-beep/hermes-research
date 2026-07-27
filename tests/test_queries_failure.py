"""Unit tests for failure-language query families and alias anchoring (pure, no network/DB)."""
import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://unused")

from pipeline import queries


class AliasTests(unittest.TestCase):
    def test_domain_reduced_to_bare_name(self):
        # Forum posts say "3plguys", not "3plguys.com" — anchoring on the FQDN misses the complaint.
        self.assertEqual(queries.aliases("problems with 3plguys.com holding stock"), ["3plguys"])

    def test_acronym_and_mixed_case_brand(self):
        self.assertEqual(queries.aliases("Does PCAC or 3PLGuys freeze funds"), ["PCAC", "3PLGuys"])

    def test_case_insensitive_dedup_keeps_first_spelling(self):
        self.assertEqual(queries.aliases("3PLGuys and 3plguys.com"), ["3PLGuys"])

    def test_limit_is_respected(self):
        self.assertEqual(len(queries.aliases("PCAC 3PLGuys RUO FDA", limit=2)), 2)

    def test_no_entities_returns_empty(self):
        self.assertEqual(queries.aliases("do sellers report frozen funds"), [])


class FailureVariantTests(unittest.TestCase):
    def test_one_query_per_family_for_primary_alias(self):
        out = queries.failure_variants("3plguys.com inventory", cap=4)
        self.assertEqual(len(out), 4)
        self.assertTrue(all(q.startswith("3plguys ") for q in out))
        # Breadth of failure MODES, not four synonyms for the same one.
        self.assertEqual([q.split(" ", 1)[1] for q in out],
                         [terms[0] for _name, terms in queries.FAILURE_FAMILIES])

    def test_second_alias_only_after_families_exhausted(self):
        out = queries.failure_variants("PCAC 3PLGuys", cap=6)
        self.assertEqual(len(out), 6)
        self.assertTrue(all(q.startswith("PCAC ") for q in out[:4]))
        self.assertTrue(all(q.startswith("3PLGuys ") for q in out[4:]))

    def test_cap_is_hard(self):
        # Cut off mid-family-sweep, so the two survivors are two DIFFERENT failure modes.
        self.assertEqual(queries.failure_variants("PCAC 3PLGuys", cap=2), [
            "PCAC reserve", "PCAC terminated"])
        self.assertEqual(queries.failure_variants("PCAC", cap=0), [])

    def test_brandless_question_falls_back_to_topic_anchor(self):
        out = queries.failure_variants("do peptide sellers lose inventory", cap=4)
        self.assertTrue(out)
        self.assertTrue(all(q.startswith("peptide sellers lose ") for q in out))

    def test_single_token_question_is_not_anchored(self):
        # "scam" hung on one word retrieves noise, not evidence.
        self.assertEqual(queries.failure_variants("peptides"), [])

    def test_deterministic(self):
        a = queries.failure_variants("3plguys.com peptides")
        b = queries.failure_variants("3plguys.com peptides")
        self.assertEqual(a, b)


class VariantsTests(unittest.TestCase):
    def test_discovery_sources_get_base_plus_failure_families(self):
        out = queries.variants("web_search", "3plguys.com peptides")
        self.assertEqual(out[0], "3plguys.com peptides")
        self.assertEqual(len(out), 1 + queries.FAILURE_QUERY_CAP)
        self.assertEqual(len(out), len(set(out)))

    def test_reddit_threads_also_pooled_with_failure_families(self):
        self.assertGreater(len(queries.variants("reddit_threads", "PCAC reserve")), 1)

    def test_anecdote_source_unchanged(self):
        out = queries.variants("hackernews", "3plguys peptides")
        self.assertEqual(len(out), 2)
        self.assertIn("anyone tried", out[1])

    def test_url_shaped_source_untouched(self):
        self.assertEqual(queries.variants("web", "https://example.com/x"),
                         ["https://example.com/x"])

    def test_empty_text(self):
        self.assertEqual(queries.variants("web_search", "  "), [""])



class FamilyTermTests(unittest.TestCase):
    def test_question_wording_picks_the_family_member(self):
        # "damaged inventory" is a different failure from "missing inventory"; searching the
        # family's default would ask about the wrong one.
        self.assertEqual(queries.family_term(("missing inventory", "lost inventory", "damaged"),
                                             "3PLs delivering damaged stock"), "damaged")

    def test_falls_back_to_the_family_default(self):
        self.assertEqual(queries.family_term(("reserve", "frozen"), "generic 3pl question"),
                         "reserve")

    def test_substring_lookalikes_do_not_match(self):
        # "undamaged" is the question NEGATING the failure; "preserve" and "scampi" are unrelated.
        for text, expected in (("shipped undamaged goods", "missing inventory"),
                               ("preserve cold chain", "missing inventory")):
            self.assertEqual(
                queries.family_term(("missing inventory", "damaged"), text), expected, text)
        self.assertEqual(queries.family_term(("scam", "avoid"), "best scampi suppliers"), "scam")


class SearchSyntaxSafetyTests(unittest.TestCase):
    """Characters that search APIs read as OPERATORS, not text. Each of these cost a whole source
    on a live run: X returned 400 Bad Request for a compressed query containing an em-dash."""

    def test_em_dash_is_removed(self):
        out = queries.compress_for_search(
            "What do operators pay a 3PL — per-order pick and pack fees?")
        self.assertNotIn("—", out)
        for dash in "‐‑‒–—―−":
            self.assertNotIn(dash, queries.compress_for_search(f"vendor {dash} reserve problems"))

    def test_intra_word_hyphens_survive(self):
        # BPC-157 and third-party are real tokens; stripping them would break entity matching.
        out = queries.compress_for_search("BPC-157 third-party COA testing by Janoshik")
        self.assertIn("BPC-157", out)
        self.assertIn("third-party", out)

    def test_leading_hyphen_is_stripped(self):
        # A leading '-' is X's NOT operator — it silently inverts the term instead of erroring.
        out = queries.compress_for_search("vendor -reserve -frozen complaints")
        self.assertNotIn("-reserve", out)
        self.assertIn("reserve", out)

    def test_dash_removal_does_not_merge_words(self):
        out = queries.compress_for_search("peptide vendors—payment processors terminated")
        self.assertNotIn("vendorspayment", out.lower())

if __name__ == "__main__":
    unittest.main()
