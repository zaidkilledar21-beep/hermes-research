"""Regression tests for the evidence-budget SQL in synthesize.load_evidence.

The query itself was untested, and it had a real defect: computing the per-source and per-thread
row numbers over the SAME raw set and then AND-ing them UNDERFILLS the budget. If a source's best 25
items are all one thread, the thread cap cuts them to 6 and every other thread's items are already
excluded by src_rank > 25 — so that source contributes 6 items where 25 were budgeted, and the outer
LIMIT cannot recover them. The fix applies the caps in sequence (thread first, then source).

Executed against in-memory sqlite rather than Postgres: sqlite implements the same window functions
and the same `IS TRUE` semantics used here, so the RANKING LOGIC is really exercised with no
database, no network, and no fixtures to maintain. What it cannot check is Postgres-specific typing;
that is what the live run does.
"""
import os
import sqlite3
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://unused")
os.environ.setdefault("OPENROUTER_API_KEY_ANALYST", "unused")

from pipeline import synthesize

SCHEMA = """
CREATE TABLE evidence_items (
    evidence_id INTEGER PRIMARY KEY,
    run_id INTEGER,
    source_id TEXT,
    grade TEXT,
    trust_tag TEXT,
    credibility_tier TEXT,
    content TEXT,
    extracted TEXT,
    author TEXT,
    thread_id TEXT,
    facet TEXT,
    answers_question INTEGER
)
"""


def _run(rows, *, per_thread, per_source, limit):
    """Run the production SQL against sqlite, translating only the placeholder style."""
    conn = sqlite3.connect(":memory:")
    conn.execute(SCHEMA)
    conn.executemany(
        "INSERT INTO evidence_items (evidence_id, run_id, source_id, grade, trust_tag, "
        "credibility_tier, content, extracted, author, thread_id, facet, answers_question) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    sql = synthesize.EVIDENCE_SQL.replace("%s", "?")
    out = conn.execute(sql, (1, per_thread, per_source, limit)).fetchall()
    conn.close()
    return [r[0] for r in out]


def _item(eid, *, source="web_search", thread=None, relevant=1, grade="B"):
    return (eid, 1, source, grade, "TRUSTED_EVIDENCE", "general_web", f"raw {eid}", f"clean {eid}",
            None, thread, None, relevant)


class EvidenceBudgetSqlTests(unittest.TestCase):
    def test_source_budget_is_spent_on_items_that_survive_the_thread_cap(self):
        # 8 items from one thread + 8 from another, all one source. With a thread cap of 2 and a
        # source cap of 4, the answer must be 4 items (2 per thread) — not 2, which is what the
        # buggy independent-ranks version returned.
        rows = [_item(i, source="reddit_threads", thread="t1") for i in range(1, 9)]
        rows += [_item(i, source="reddit_threads", thread="t2") for i in range(9, 17)]
        got = _run(rows, per_thread=2, per_source=4, limit=60)
        self.assertEqual(len(got), 4)
        self.assertEqual(got, [1, 2, 9, 10])

    def test_thread_cap_applies_only_to_threaded_items(self):
        # Postgres puts every NULL thread_id in ONE partition; if the cap were applied there it
        # would silently limit the entire open web to per_thread items.
        rows = [_item(i) for i in range(1, 11)]
        self.assertEqual(len(_run(rows, per_thread=2, per_source=25, limit=60)), 10)

    def test_per_source_cap_stops_one_source_owning_the_budget(self):
        rows = [_item(i, source="reddit_threads", thread=f"t{i}") for i in range(1, 40)]
        rows += [_item(100 + i, source="web_search") for i in range(5)]
        got = _run(rows, per_thread=6, per_source=10, limit=60)
        self.assertEqual(len(got), 15)
        self.assertEqual(sum(1 for e in got if e < 100), 10)

    def test_relevance_outranks_grade(self):
        # grade measures RETRIEVAL fidelity, not usefulness: a grade-A irrelevant GitHub repo must
        # not outrank a grade-B web page that answers the question.
        rows = [_item(1, source="github_api", grade="A", relevant=0),
                _item(2, source="web_search", grade="B", relevant=1),
                _item(3, source="web_search", grade="C", relevant=None)]
        self.assertEqual(_run(rows, per_thread=6, per_source=25, limit=60), [2, 3, 1])

    def test_off_topic_items_are_kept_as_last_resort_not_deleted(self):
        # If the extractor over-rejects, a run must still have something to reason over.
        rows = [_item(1, relevant=0), _item(2, relevant=0)]
        self.assertEqual(_run(rows, per_thread=6, per_source=25, limit=60), [1, 2])

    def test_global_limit_still_applies(self):
        rows = [_item(i, source=f"src{i % 5}", thread=f"t{i}") for i in range(1, 60)]
        self.assertEqual(len(_run(rows, per_thread=6, per_source=25, limit=10)), 10)

    def test_tombstoned_items_are_excluded(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(SCHEMA)
        conn.execute(
            "INSERT INTO evidence_items (evidence_id, run_id, source_id, grade, content, "
            "answers_question) VALUES (1, 1, 'web_search', 'B', NULL, 1)")
        sql = synthesize.EVIDENCE_SQL.replace("%s", "?")
        self.assertEqual(conn.execute(sql, (1, 6, 25, 60)).fetchall(), [])
        conn.close()


if __name__ == "__main__":
    unittest.main()
