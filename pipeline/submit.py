"""Create a research run. Usage:
  python -m pipeline.submit --question "..." [--sources x,github,web,reddit_reach] [--by cli]
Prints the new run_id. Then:  python -m pipeline.run --run <id>
(Hermes/Telegram path calls request_run() directly instead of this CLI.)
"""
from __future__ import annotations
import argparse
import json
import os

# Before DATABASE_URL and before decompose is imported (it reads DECOMPOSE_* at module scope).
# request_run() is called in-process by the web app, so it inherits uvicorn's environment.
from pipeline import envfile  # noqa: E402
envfile.load()

import psycopg  # noqa: E402

DATABASE_URL = os.environ["DATABASE_URL"]


def request_run(question: str, sources: list[str], by: str = "owner") -> int:
    """Create a run and its sub-questions. Every caller path (Hermes chat's /api/ask, the web
    /ask form, and the bare CLI) goes through here, which is why decomposition is wired in AT
    THIS LEVEL rather than left as a skill a chat model might remember to invoke separately.

    `sources` is the caller's floor (ALWAYS_SOURCES unioned with anything explicitly requested,
    for the web app; whatever --sources named, for the CLI). decompose.py may propose additional
    per-facet sources; it never gets to drop this floor from any resulting sub-question — see
    pipeline/decompose.py's module docstring for why that's the same discipline plan_queries.py
    already uses.
    """
    from pipeline import decompose

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        run_id = conn.execute(
            "INSERT INTO research_runs (question, submitted_by, status) VALUES (%s,%s,'decomposing') "
            "RETURNING run_id", (question, by)).fetchone()[0]

        subqs, telemetry = decompose.decompose(question, run_id=run_id)
        for text, extra in subqs:
            plan = sorted(set(sources) | set(extra))
            conn.execute(
                "INSERT INTO sub_questions (run_id, text, source_plan) VALUES (%s,%s,%s)",
                (run_id, text, json.dumps(plan)))

        if telemetry.get("model") and telemetry.get("cost", 0):
            from collectors import common
            common.log_agent_run(run_id, "analyst", telemetry["model"],
                                 telemetry.get("tokens_in", 0), telemetry.get("tokens_out", 0),
                                 telemetry.get("cost", 0.0), skill="decompose")
        # Positive signal (lesson #26): visible without a DB client, and distinguishes a genuine
        # single-facet question from the pre-decompose fallback that used to look identical.
        conn.execute(
            "UPDATE research_runs SET notes = concat_ws(E'\\n', notes, %s::text) WHERE run_id=%s",
            (f"decompose: {telemetry['label']}", run_id))
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
