"""Fact-level vertical memory — the registry pattern extended from venues to figures (v3 Part L).

An operator who has watched a vertical for years carries baselines: what a normal reserve
percentage is, what normal fulfilment pricing looks like. When a number deviates, they NOTICE.
This module gives the engine the crudest useful version of that: accepted findings' structured
figures (Part G) accumulate in `vertical_facts` keyed by (vertical, subject, unit), where the
vertical is the same deterministic topic fingerprint the venue registry uses
(registry.topic_key). Once >= PRIORS_MIN_N observations exist for a key, a new figure deviating
more than PRIORS_SURPRISE_FACTOR x from the stored MEDIAN generates one flag finding —
"surprising vs prior research" — that faces the reviewers and the gate like any other claim.

Discipline, all inherited from the registry (the proven template):
  - accepted-only writes (lesson #27 — quarantined figures must not teach the memory),
  - idempotent per finding (UNIQUE constraint; re-running a stage cannot double-count),
  - age-out (observations older than PRIORS_MAX_AGE_DAYS leave the median),
  - fail-soft everywhere with a positive signal in notes (lesson #26),
  - priors NEVER auto-reject — they only flag; reviewers judge the flag,
  - cold-start honest: below MIN_N the module stays silent, and silence is recorded as
    "insufficient priors", never presented as "nothing surprising".
"""
from __future__ import annotations
import os
import sys

DATABASE_URL = os.environ["DATABASE_URL"]
MIN_N = int(os.environ.get("PRIORS_MIN_N", "5"))
SURPRISE_FACTOR = float(os.environ.get("PRIORS_SURPRISE_FACTOR", "3"))
MAX_AGE_DAYS = int(os.environ.get("PRIORS_MAX_AGE_DAYS", "180"))
_MARKER = "prior-surprise"


def surprising(value: float, median: float, factor: float = SURPRISE_FACTOR) -> bool:
    """Pure deviation test: value differs from the median by more than `factor` in either
    direction. Zero/negative medians are never judged (a ratio across zero means nothing)."""
    if median <= 0 or value <= 0:
        return False
    return value / median > factor or median / value > factor


def record_run(run_id: int) -> int:
    """After the gate: accepted findings' figures land in vertical_facts. Returns rows written."""
    import psycopg
    from pipeline import registry

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        question = conn.execute("SELECT question FROM research_runs WHERE run_id=%s",
                                (run_id,)).fetchone()
        if not question:
            return 0
        vertical = registry.topic_key(question[0])
        rows = conn.execute(
            "SELECT finding_id, figures FROM findings WHERE run_id=%s "
            "AND disposition='accepted' AND figures IS NOT NULL", (run_id,)).fetchall()
        written = 0
        for fid, figs in rows:
            for fig in (figs if isinstance(figs, list) else []):
                if not isinstance(fig, dict):
                    continue
                try:
                    value = float(fig["value"])
                    subject = str(fig["subject"]).lower()
                    unit = str(fig["unit"]).lower()
                except (KeyError, TypeError, ValueError):
                    continue
                if not subject or not unit:
                    continue
                cur = conn.execute(
                    "INSERT INTO vertical_facts (vertical, subject, unit, value, run_id, "
                    "finding_id) VALUES (%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (finding_id, subject, unit) DO NOTHING",
                    (vertical, subject, unit, value, run_id, fid))
                written += cur.rowcount or 0
    return written


def check_run(run_id: int) -> tuple[int, int]:
    """Before review: compare this run's figures against stored priors from OTHER runs in the
    same vertical. Returns (flags_inserted, subjects_below_min_n). A flag is one observed finding
    citing the same evidence as the surprising figure's finding — reviewers judge it."""
    import psycopg
    from pipeline import registry

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        question = conn.execute("SELECT question FROM research_runs WHERE run_id=%s",
                                (run_id,)).fetchone()
        if not question:
            return 0, 0
        vertical = registry.topic_key(question[0])
        rows = conn.execute(
            "SELECT finding_id, evidence_ids, figures FROM findings WHERE run_id=%s "
            "AND figures IS NOT NULL "
            "AND COALESCE(disposition,'') NOT IN ('superseded_by_revision')",
            (run_id,)).fetchall()
        flags = 0
        thin = 0
        for fid, ev_ids, figs in rows:
            for fig in (figs if isinstance(figs, list) else []):
                if not isinstance(fig, dict):
                    continue
                try:
                    value = float(fig["value"])
                    subject = str(fig["subject"]).lower()
                    unit = str(fig["unit"]).lower()
                except (KeyError, TypeError, ValueError):
                    continue
                stat = conn.execute(
                    "SELECT count(*), percentile_cont(0.5) WITHIN GROUP (ORDER BY value), "
                    "count(DISTINCT run_id) FROM vertical_facts "
                    "WHERE vertical=%s AND subject=%s AND unit=%s AND run_id <> %s "
                    "AND observed_at > now() - make_interval(days => %s)",
                    (vertical, subject, unit, run_id, MAX_AGE_DAYS)).fetchone()
                n, median, n_runs = stat[0], stat[1], stat[2]
                if n < MIN_N:
                    thin += 1
                    continue
                if median is None or not surprising(value, float(median)):
                    continue
                claim = (f"Surprising vs prior research: {value:g} {unit} for {subject}; "
                         f"the median of {n} prior observations across {n_runs} runs in this "
                         f"vertical was {float(median):g} {unit}. Deviation this large usually "
                         f"means a different product tier, a different time period, or an error "
                         f"— worth resolving before relying on either number.")
                # Idempotent per (run, finding, subject): skip if an identical flag exists.
                exists = conn.execute(
                    "SELECT 1 FROM findings WHERE run_id=%s AND disposition_detail=%s",
                    (run_id, f"{_MARKER}: finding {fid} {subject}/{unit}")).fetchone()
                if exists:
                    continue
                conn.execute(
                    "INSERT INTO findings (run_id, claim, label, evidence_ids, disposition, "
                    "disposition_detail) VALUES (%s,%s,'observed',%s,'accepted',%s)",
                    (run_id, claim, list(ev_ids or []),
                     f"{_MARKER}: finding {fid} {subject}/{unit}"))
                flags += 1
        if flags or thin:
            print(f"[priors] {flags} surprise flag(s), {thin} figure(s) below MIN_N={MIN_N} "
                  f"for run {run_id}", file=sys.stderr)
        return flags, thin
