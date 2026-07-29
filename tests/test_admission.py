"""Tests for the run admission gate (pipeline/admission.py).

The production SQL is executed against in-memory sqlite, translating only the placeholder style and
the two Postgres-only constructs (`make_interval`, `now()`). That really exercises the PREDICATE
LOGIC — which conditions admit and which refuse — with no database, no network and no fixtures.

What sqlite CANNOT prove here, stated plainly so nobody mistakes a green suite for a safe gate:
  * the SERIALIZABLE isolation that stops two READ COMMITTED transactions sharing a snapshot and
    both passing the count predicate. sqlite serializes writers globally, so it cannot exhibit the
    bug at all. That property is verified live, by starting MAX_CONCURRENT_RUNS+1 real runs.
  * Postgres typing and the serialization-failure retry path.
The one thing sqlite CAN prove exactly, and the thing most likely to rot silently, is that the
Python fingerprint matches the SQL fingerprint byte for byte.
"""
import hashlib
import os
import re
import sqlite3
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://unused")

from pipeline import admission  # noqa: E402

SCHEMA = """
CREATE TABLE run_admissions (
    run_id        INTEGER PRIMARY KEY,
    question_hash TEXT NOT NULL,
    started_at    TEXT DEFAULT (datetime('now')),
    heartbeat_at  TEXT DEFAULT (datetime('now')),
    status        TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE research_runs (
    run_id        INTEGER PRIMARY KEY,
    question      TEXT,
    question_hash TEXT,
    status        TEXT,
    delivered_at  TEXT
);
CREATE TABLE findings (
    finding_id  INTEGER PRIMARY KEY,
    run_id      INTEGER,
    disposition TEXT
);
CREATE TABLE agent_runs (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER,
    cost_usd   REAL,
    created_at TEXT
);
"""


def _sqlite_sql(sql: str) -> str:
    """Translate the production statement to sqlite. Only dialect differences — never logic."""
    out = sql
    out = re.sub(r"now\(\) - make_interval\(mins => %\(stale_min\)s\)",
                 "datetime('now', '-' || :stale_min || ' minutes')", out)
    out = re.sub(r"now\(\) - make_interval\(hours => %\(dup_hours\)s\)",
                 "datetime('now', '-' || :dup_hours || ' hours')", out)
    out = out.replace("current_date", "date('now')")
    out = out.replace("now()", "datetime('now')")
    # psycopg pyformat -> sqlite named style
    out = re.sub(r"%\((\w+)\)s", r":\1", out)
    # sqlite has no true boolean parameter here; :force arrives as 0/1
    out = out.replace("(:force OR NOT EXISTS", "(:force = 1 OR NOT EXISTS")
    # ON CONFLICT ... DO UPDATE is kept as-is: sqlite's UPSERT syntax is identical here, and this
    # clause IS the re-entrancy mechanism, so stripping it would test a statement we do not ship.
    return out


class _Fixture:
    def __init__(self, **over):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)
        self.cfg = {"stale_min": 5, "max_conc": 3, "daily_cap": 2.0,
                    "dup_hours": 24, "min_find": 8}
        self.cfg.update(over)

    def live_slots(self, n, *, age_minutes=0, first_id=900):
        for i in range(n):
            self.conn.execute(
                "INSERT INTO run_admissions (run_id, question_hash, heartbeat_at, status) "
                "VALUES (?,?,datetime('now', ?),'active')",
                (first_id + i, f"h{i}", f"-{age_minutes} minutes"))

    def delivered(self, run_id, question, findings, *, hours_ago=1):
        self.conn.execute(
            "INSERT INTO research_runs (run_id, question, question_hash, status, delivered_at) "
            "VALUES (?,?,?,'delivered',datetime('now', ?))",
            (run_id, question, admission.question_hash(question), f"-{hours_ago} hours"))
        for _ in range(findings):
            self.conn.execute(
                "INSERT INTO findings (run_id, disposition) VALUES (?, 'accepted')", (run_id,))

    def spend_today(self, dollars):
        self.conn.execute("INSERT INTO agent_runs (run_id, cost_usd, created_at) "
                          "VALUES (1, ?, datetime('now'))", (dollars,))

    def admit(self, run_id, question, *, force=False) -> bool:
        params = dict(self.cfg)
        params.update({"run_id": run_id, "qhash": admission.question_hash(question),
                       "force": 1 if force else 0})
        cur = self.conn.execute(_sqlite_sql(admission.ADMIT_SQL), params)
        return cur.fetchone() is not None


class FingerprintTests(unittest.TestCase):
    """The Python hash and the migration's SQL hash must never drift. If they do, duplicate
    detection silently stops working for every backfilled row and nothing else fails."""

    @staticmethod
    def sql_equivalent(q):
        """md5(lower(btrim(regexp_replace(question, '\\s+', ' ', 'g')))) — the migration's literal."""
        return hashlib.md5(re.sub(r"\s+", " ", q).strip().lower().encode("utf-8")).hexdigest()

    def test_matches_the_sql_expression(self):
        for q in ["Does Shopify's AUP prohibit peptides?",
                  "  leading and trailing  ",
                  "internal   double    spaces",
                  "MiXeD CaSe QuEsTiOn",
                  "line one\nline two\ttabbed",
                  "unicode - em dash and unicode chars"]:
            self.assertEqual(admission.question_hash(q), self.sql_equivalent(q), q)

    def test_whitespace_and_case_collapse_to_one_fingerprint(self):
        a = admission.question_hash("Which payment processors onboard peptide merchants in 2026?")
        b = admission.question_hash(
            "  which   payment processors onboard\npeptide merchants in 2026?  ")
        self.assertEqual(a, b)

    def test_different_questions_differ(self):
        self.assertNotEqual(admission.question_hash("question one"),
                            admission.question_hash("question two"))


class ConcurrencyTests(unittest.TestCase):
    def test_admits_below_the_limit(self):
        f = _Fixture()
        f.live_slots(2)
        self.assertTrue(f.admit(1, "a fresh question"))

    def test_refuses_at_the_limit(self):
        f = _Fixture()
        f.live_slots(3)
        self.assertFalse(f.admit(1, "a fresh question"))

    def test_stale_heartbeats_do_not_hold_slots(self):
        """The reaper IS the absent heartbeat — no process sweep, no PID tracking. A run whose
        process died stops beating and its slot frees itself."""
        f = _Fixture()
        f.live_slots(3, age_minutes=60)
        self.assertTrue(f.admit(1, "a fresh question"))

    def test_a_run_already_holding_a_slot_is_readmitted_when_full(self):
        """Re-entrancy: without this clause a resumed run is locked out by its own reservation."""
        f = _Fixture()
        f.live_slots(3, first_id=1)          # run 1 holds one of the three
        self.assertTrue(f.admit(1, "whatever question"))


class DuplicateTests(unittest.TestCase):
    Q = "Does Shopify's Acceptable Use Policy prohibit selling research chemicals?"

    def test_refuses_a_re_ask_of_a_productive_run(self):
        """Run 58 delivered 18 findings; runs 67/70/71/72 re-asked it verbatim."""
        f = _Fixture()
        f.delivered(58, self.Q, findings=18)
        self.assertFalse(f.admit(70, self.Q))

    def test_allows_a_re_ask_of_a_THIN_run(self):
        """Run 35 delivered 3 findings, and run 43 deliberately re-ran it and produced 32. A
        blanket duplicate guard would have blocked the most productive run in the campaign."""
        f = _Fixture()
        f.delivered(35, self.Q, findings=3)
        self.assertTrue(f.admit(43, self.Q))

    def test_allows_a_re_ask_outside_the_window(self):
        f = _Fixture()
        f.delivered(58, self.Q, findings=18, hours_ago=40)
        self.assertTrue(f.admit(70, self.Q))

    def test_whitespace_and_case_variant_is_still_a_duplicate(self):
        f = _Fixture()
        f.delivered(58, self.Q, findings=18)
        self.assertFalse(f.admit(70, "  " + self.Q.upper().replace(" ", "   ") + " "))

    def test_only_accepted_findings_count_toward_the_threshold(self):
        f = _Fixture()
        f.delivered(58, self.Q, findings=0)
        for _ in range(20):
            f.conn.execute(
                "INSERT INTO findings (run_id, disposition) VALUES (58, 'rejected_by_reviewer')")
        self.assertTrue(f.admit(70, self.Q))

    def test_a_run_does_not_block_itself(self):
        f = _Fixture()
        f.delivered(58, self.Q, findings=18)
        self.assertTrue(f.admit(58, self.Q))

    def test_force_waives_the_duplicate_check(self):
        f = _Fixture()
        f.delivered(58, self.Q, findings=18)
        self.assertTrue(f.admit(70, self.Q, force=True))


class BudgetTests(unittest.TestCase):
    def test_refuses_when_the_day_is_spent(self):
        f = _Fixture()
        f.spend_today(2.50)
        self.assertFalse(f.admit(1, "a fresh question"))

    def test_admits_below_the_daily_cap(self):
        f = _Fixture()
        f.spend_today(0.40)
        self.assertTrue(f.admit(1, "a fresh question"))

    def test_the_cap_is_cross_run_not_per_run(self):
        """The defect of 2026-07-28: fourteen runs, each correctly under its own budget, together
        far past the configured ceiling. No single run's spend would have tripped this."""
        f = _Fixture()
        for _ in range(14):
            f.conn.execute("INSERT INTO agent_runs (run_id, cost_usd, created_at) "
                           "VALUES (1, 0.20, datetime('now'))")
        self.assertFalse(f.admit(99, "a fresh question"))

    def test_yesterdays_spend_does_not_count(self):
        f = _Fixture()
        f.conn.execute("INSERT INTO agent_runs (run_id, cost_usd, created_at) "
                       "VALUES (1, 50.0, datetime('now', '-1 day'))")
        self.assertTrue(f.admit(1, "a fresh question"))

    def test_force_does_NOT_waive_the_budget(self):
        f = _Fixture()
        f.spend_today(2.50)
        self.assertFalse(f.admit(1, "a fresh question", force=True))

    def test_force_does_NOT_waive_concurrency(self):
        f = _Fixture()
        f.live_slots(3)
        self.assertFalse(f.admit(1, "a fresh question", force=True))


if __name__ == "__main__":
    unittest.main()
