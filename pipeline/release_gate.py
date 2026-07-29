"""Deterministic release gate. CODE, not a model — this is the integrity backstop.

A research report may only be delivered if it passes every check here. The synthesis
model proposes findings; this gate disposes. Run:  python -m pipeline.release_gate --run <id>
Exit 0 = APPROVED_FOR_DELIVERY. Non-zero = blocked, with reasons on stderr.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
import psycopg

DATABASE_URL = os.environ["DATABASE_URL"]
DAILY_CAP_USD = float(os.environ.get("OPENROUTER_DAILY_CAP_USD", "2"))
RUN_CAP_USD = float(os.environ.get("OPENROUTER_RUN_CAP_USD", "2"))


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).casefold().strip()


def quote_anchored(quote: str | None, evidence_texts: list[str]) -> bool | None:
    """v3 M1 — content-level citation check. None = no quote supplied (tolerated: findings from
    before the quote requirement, and non-observed labels, carry none). True/False = a quote was
    supplied and does / does not appear verbatim (whitespace/case-normalized) in the cited text.
    A paraphrase is indistinguishable from a fabrication here ON PURPOSE — the prompt demands a
    character-for-character copy precisely so this check stays deterministic."""
    if not quote or not str(quote).strip():
        return None
    needle = _normalize(str(quote))
    if len(needle) < 10:
        return False
    return any(needle in _normalize(t) for t in evidence_texts)


def check(run_id: int) -> list[str]:
    """Set per-finding dispositions and return only systemic blocking problems."""
    problems: list[str] = []
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        run = conn.execute(
            "SELECT synthesis_state FROM research_runs WHERE run_id=%s", (run_id,)
        ).fetchone()
        synthesis_state = run[0] if run else None
        if synthesis_state not in ("ok", "valid_empty"):
            problems.append(
                f"synthesis state is '{synthesis_state or 'missing'}', not ok or valid_empty"
            )

        findings = conn.execute(
            "SELECT finding_id, label, evidence_ids, disposition, disposition_detail, quote "
            "FROM findings WHERE run_id=%s ORDER BY finding_id", (run_id,)
        ).fetchall()
        # Text is loaded for the quote-anchor check (M1) — the SAME text synthesis and the
        # reviewers read (COALESCE(extracted, content)), else a quote copied faithfully from the
        # extracted view could fail against raw boilerplate it never saw.
        ev_rows = conn.execute(
            "SELECT evidence_id, trust_tag, COALESCE(extracted, content) "
            "FROM evidence_items WHERE run_id=%s", (run_id,)
        ).fetchall()
        valid_ev = {r[0] for r in ev_rows}
        ev_text = {r[0]: (r[2] or "") for r in ev_rows}

        rejects: dict[int, list[tuple[str, str | None]]] = {}
        for reviewer, fid, detail in conn.execute(
            "SELECT reviewer, finding_id, detail FROM reviews "
            "WHERE run_id=%s AND severity='reject' ORDER BY finding_id, reviewer, review_id",
            (run_id,),
        ).fetchall():
            if fid is not None:
                rejects.setdefault(fid, []).append((reviewer, detail))

        accepted = 0
        quarantined_or_rejected = 0
        superseded = 0
        valid_labels = {"observed", "inferred", "unknown", "community_signal"}
        for fid, label, ev_ids, old_disposition, old_detail, quote in findings:
            evidence_ids = list(ev_ids or [])
            fabricated = [e for e in evidence_ids if e not in valid_ev]
            # v3 revision loop: an original replaced by a revision is LINEAGE, not a verdict to
            # re-litigate. Its reject reviews still exist, so without this skip the gate would
            # clobber 'superseded_by_revision' back to 'rejected_by_reviewer' and the report would
            # state the same claim twice — once withheld, once revised. Excluded from both tallies:
            # the revision row is the one that counts.
            if old_disposition == "superseded_by_revision":
                superseded += 1
                continue
            anchored = quote_anchored(quote, [ev_text[e] for e in evidence_ids if e in ev_text])
            if fid in rejects:
                disposition = "rejected_by_reviewer"
                detail = "; ".join(
                    f"{reviewer}: {review_detail or 'unsupported'}"
                    for reviewer, review_detail in rejects[fid]
                )
            elif old_disposition == "quarantined_invalid_label" or label not in valid_labels:
                disposition = "quarantined_invalid_label"
                detail = old_detail or f"original_label={json.dumps(label)}"
            elif fabricated:
                disposition = "quarantined_fabricated_ids"
                detail = json.dumps(fabricated)
            elif label in ("observed", "inferred", "community_signal") and not evidence_ids:
                disposition = "quarantined_no_evidence"
                detail = f"label={label}; evidence_ids=[]"
            elif anchored is False:
                # v3 M1: a quote was SUPPLIED and does not appear in the cited evidence — the
                # content-level analogue of a fabricated id. Missing quotes (None) pass: findings
                # predating the requirement and non-observed labels carry none.
                disposition = "quarantined_unanchored_quote"
                detail = f"quote not found verbatim in cited evidence: {str(quote)[:120]}"
            else:
                disposition = "accepted"
                detail = None
            conn.execute(
                "UPDATE findings SET disposition=%s, disposition_detail=%s WHERE finding_id=%s",
                (disposition, detail, fid),
            )
            if disposition == "accepted":
                accepted += 1
            else:
                quarantined_or_rejected += 1

        judged = len(findings) - superseded  # superseded rows are lineage, not verdicts
        if judged and quarantined_or_rejected * 2 > judged:
            problems.append(
                f"{quarantined_or_rejected} of {judged} findings are quarantined or rejected"
            )
        if accepted == 0 and not (synthesis_state == "valid_empty" and not findings):
            problems.append("zero accepted findings remain")

        # Spending is stopped upstream; delivery remains useful even when telemetry shows overrun.
        spent = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM agent_runs WHERE run_id=%s", (run_id,)
        ).fetchone()[0]
        # `spent` is this ONE run's cost, so it belongs against the per-run ceiling. It used to be
        # compared to DAILY_CAP_USD — the same conflation that made the daily cap a per-run cap
        # everywhere else. The day's total is reported separately because the two overrun for
        # different reasons: one run being expensive, versus many runs being cheap at once.
        if float(spent) > RUN_CAP_USD:
            print(f"WARN: run cost ${float(spent):.4f} exceeds run cap ${RUN_CAP_USD:.2f}",
                  file=sys.stderr)
        day_total = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM agent_runs WHERE created_at >= current_date"
        ).fetchone()[0]
        if float(day_total) > DAILY_CAP_USD:
            print(f"WARN: today's total across all runs ${float(day_total):.4f} exceeds daily cap "
                  f"${DAILY_CAP_USD:.2f}", file=sys.stderr)

    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, required=True)
    a = ap.parse_args()
    problems = check(a.run)
    if problems:
        print(f"RELEASE BLOCKED for run {a.run}:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        # Mark the run blocked so the owner surface shows why.
        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            conn.execute("UPDATE research_runs SET status='gated', notes=%s WHERE run_id=%s",
                         ("; ".join(problems), a.run))
        return 2
    print(f"APPROVED_FOR_DELIVERY: run {a.run} passed all integrity checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
