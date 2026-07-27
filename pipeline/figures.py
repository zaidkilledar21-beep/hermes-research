"""Figure cross-check — conflicting numbers are forced to MEET each other (v3 Part G).

A $99/month price finding and a $350/month price finding for the same thing used to sit in
different report sections, never compared. Synthesis now emits structured figures
([{value, unit, subject}]) alongside claims; this module groups them by (unit, subject) across a
run's live findings and, when the spread within a group exceeds FIGURES_SPREAD_FACTOR (default
3x), inserts ONE new observed finding stating the conflict with citations to both sides'
evidence. It is inserted BEFORE review, so the reviewers judge the conflict claim like any other
finding, and the gate treats it identically.

Deterministic, $0, no model call. Not a modeling engine: it guarantees conflicting figures are
STATED, not reconciled — reconciliation is the model-economics Hermes skill's job, downstream.
"""
from __future__ import annotations
import json
import os
import sys

DATABASE_URL = os.environ["DATABASE_URL"]
SPREAD_FACTOR = float(os.environ.get("FIGURES_SPREAD_FACTOR", "3"))
_MARKER = "figure-cross-check"


def conflicts(figured: list[tuple[int, list[int], dict]]) -> list[dict]:
    """Group (finding_id, evidence_ids, figure) rows by (unit, subject); return groups whose
    max/min spread exceeds SPREAD_FACTOR. Pure — no DB. Zero and sign-crossing values are skipped
    (a ratio across zero is meaningless, not alarming)."""
    groups: dict[tuple[str, str], list[tuple[int, list[int], float]]] = {}
    for fid, ev_ids, fig in figured:
        try:
            value = float(fig["value"])
        except (KeyError, TypeError, ValueError):
            continue
        key = (str(fig.get("unit", "")).lower(), str(fig.get("subject", "")).lower())
        if not key[0] or not key[1]:
            continue
        groups.setdefault(key, []).append((fid, ev_ids or [], value))
    out = []
    for (unit, subject), rows in groups.items():
        values = [v for _, _, v in rows]
        if len(rows) < 2 or min(values) <= 0:
            continue
        if max(values) / min(values) > SPREAD_FACTOR:
            lo = min(rows, key=lambda r: r[2])
            hi = max(rows, key=lambda r: r[2])
            out.append({"unit": unit, "subject": subject, "low": lo, "high": hi,
                        "n": len(rows)})
    return out


def cross_check(run_id: int) -> int:
    """Insert one conflict finding per excessive-spread group. Idempotent per synthesis pass:
    prior cross-check findings are superseded first (re-synthesis supersedes everything anyway;
    this covers the same-pass re-run case). Returns findings inserted. Fail-soft at the caller."""
    import psycopg
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT finding_id, evidence_ids, figures FROM findings "
            "WHERE run_id=%s AND figures IS NOT NULL "
            "AND COALESCE(disposition,'') NOT IN ('superseded_by_revision')", (run_id,)).fetchall()
        figured = []
        for fid, ev_ids, figs in rows:
            figs = figs if isinstance(figs, list) else []
            for fig in figs:
                if isinstance(fig, dict):
                    figured.append((fid, list(ev_ids or []), fig))
        found = conflicts(figured)
        if not found:
            return 0
        conn.execute(
            "UPDATE findings SET disposition='superseded_by_revision', "
            "disposition_detail='superseded by re-run figure cross-check' "
            "WHERE run_id=%s AND disposition_detail LIKE %s", (run_id, f"{_MARKER}%"))
        n = 0
        for c in found:
            lo_fid, lo_ev, lo_v = c["low"]
            hi_fid, hi_ev, hi_v = c["high"]
            claim = (f"Evidence contains conflicting figures for {c['subject']} "
                     f"({c['unit']}): {lo_v:g} vs {hi_v:g} — a "
                     f"{hi_v / lo_v:.1f}x spread across {c['n']} findings. Both cannot describe "
                     f"the same thing; the report should treat this range as unresolved.")
            conn.execute(
                "INSERT INTO findings (run_id, claim, label, evidence_ids, disposition, "
                "disposition_detail) VALUES (%s,%s,'observed',%s,'accepted',%s)",
                (run_id, claim, sorted(set(lo_ev + hi_ev)),
                 f"{_MARKER}: findings {lo_fid} vs {hi_fid}"))
            n += 1
        print(f"[figures] {n} conflicting figure group(s) flagged for run {run_id}",
              file=sys.stderr)
        return n
