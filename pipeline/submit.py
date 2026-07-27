"""Create a research run. Usage:
  python -m pipeline.submit --question "..." [--sources x,github,web,reddit_reach] [--by cli]
Prints the new run_id. Then:  python -m pipeline.run --run <id>
(Hermes/Telegram path calls request_run() directly instead of this CLI.)
"""
from __future__ import annotations
import argparse
import os
import psycopg

DATABASE_URL = os.environ["DATABASE_URL"]


def request_run(question: str, sources: list[str], by: str = "owner") -> int:
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        run_id = conn.execute(
            "INSERT INTO research_runs (question, submitted_by, status) VALUES (%s,%s,'decomposing') "
            "RETURNING run_id", (question, by)).fetchone()[0]
        conn.execute(
            "INSERT INTO sub_questions (run_id, text, source_plan) VALUES (%s,%s,%s)",
            (run_id, question, __import__("json").dumps(sources)))
    return run_id


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", required=True)
    ap.add_argument("--sources", default="x,github,web",
                    help="comma list: x,github,youtube,rss,web,reddit_reach,instagram_reach,facebook_reach")
    ap.add_argument("--by", default="cli")
    a = ap.parse_args()
    rid = request_run(a.question, [s.strip() for s in a.sources.split(",") if s.strip()], a.by)
    print(rid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
