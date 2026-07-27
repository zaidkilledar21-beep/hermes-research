"""Capability scorecard — the deterministic gap instrument for the v3 upgrade (plan Part I).

Turns "are we closer to expert-level research" into numbers computed from the database alone:
no model calls, no network beyond Postgres, $0. Run it before a change to freeze a baseline and
after to compare; the comparison is the argument, an absolute score means nothing on its own
(same discipline as evals/score.py).

Metrics, each mapped to a gap in the v3 plan's Part 0 table:
  aim                 relevant/extracted per run          (gap 2 — do we retrieve what answers)
  extraction_liveness fraction of runs with extracted>0   (gap 3 — the stage that silently dies)
  specificity         fraction of accepted findings that carry a named entity AND a figure/date
                                                          (gaps 4/5 — specifics, not categories)
  brief_retention     named entities in the consolidated brief / named entities in its runs'
                      accepted findings                   (gap 5 — consolidation must not average)
  repair_rate         revised-and-accepted / reviewer-rejected (gap 6 — 0 until Part E lands)
  gap_closure         round-1 subs derived from round-0 unknowns that produced accepted findings
                                                          (gap 7 — 0 until Part F lands)
  subs_per_run        decomposition depth                 (gap 12 — 1.0 = undecomposed)
  screening           per-run PRISMA-style flow ledger (M2): retrieved -> extracted -> relevant ->
                      considered -> cited, with exclusion reasons

Columns added by later migrations (revision_round, sub_questions.round) may not exist yet when the
baseline is frozen — every such query degrades to None rather than failing, so the instrument runs
identically before and after the schema catches up.

Usage:
  python -m evals.capability_metrics --runs 29-42                 # print scorecard JSON
  python -m evals.capability_metrics --runs 29-42 --out evals/baseline-capability.json
  python -m evals.capability_metrics --runs 29-42 --baseline evals/baseline-capability.json
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys

# The entity heuristic mirrors queries._is_entity's shape logic (mid-word capital, acronym,
# alnum mix, domain) plus TitleCase-not-sentence-initial. It is a PROXY: stable across runs, so
# before/after movement is meaningful even though the absolute count is fuzzy.
_DOMAIN = re.compile(r"\b[a-z0-9][a-z0-9-]*\.(?:com|net|org|io|co|us|shop|store|xyz|ai|app)\b", re.I)
_FIGURE = re.compile(r"\$\s?[\d][\d,]*(?:\.\d+)?|\b\d+(?:\.\d+)?\s?%|\b(?:19|20)\d{2}\b")
_WORD = re.compile(r"[A-Za-z0-9][\w&'.-]*")


def _is_entity_token(tok: str, sentence_initial: bool) -> bool:
    if _DOMAIN.fullmatch(tok):
        return True
    if any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):  # BPC-157, 3PL
        return True
    if tok.isupper() and len(tok) > 1:                                    # FDA, RUO
        return True
    if re.search(r"[A-Z]", tok[1:]):                                      # 3PLGuys, LegitScript
        return True
    if tok[:1].isupper() and tok[1:].islower() and not sentence_initial:  # Newtropin mid-sentence
        return True
    return False


def named_entities(text: str) -> set[str]:
    """Case-preserving set of entity-shaped tokens; sentence-initial TitleCase excluded."""
    out: set[str] = set()
    for sentence in re.split(r"[.!?]\s+|\n+", text or ""):
        for i, m in enumerate(_WORD.finditer(sentence)):
            tok = m.group(0).strip(".,;:'")
            if len(tok) >= 2 and _is_entity_token(tok, sentence_initial=(i == 0)):
                out.add(tok.lower())
    return out


def has_figure(text: str) -> bool:
    return bool(_FIGURE.search(text or ""))


def is_specific(claim: str) -> bool:
    return bool(named_entities(claim)) and has_figure(claim)


# ── DB access ─────────────────────────────────────────────────────────────────────────────────────

def _connect():
    import psycopg
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)


def _maybe(conn, sql: str, params=()):
    """Run a query that may reference not-yet-migrated columns; None means 'schema not there yet',
    which the report shows honestly instead of crashing the whole scorecard."""
    try:
        return conn.execute(sql, params).fetchall()
    except Exception:
        return None


def run_metrics(conn, run_ids: list[int]) -> dict:
    ph = ",".join(["%s"] * len(run_ids))
    runs: dict[int, dict] = {}

    for rid, retrieved, extracted, relevant, cited_pool in conn.execute(
        f"""SELECT r.run_id,
                   count(e.evidence_id),
                   count(e.extracted),
                   count(*) FILTER (WHERE e.answers_question IS TRUE),
                   count(*) FILTER (WHERE e.content IS NOT NULL)
            FROM research_runs r LEFT JOIN evidence_items e ON e.run_id = r.run_id
            WHERE r.run_id IN ({ph}) GROUP BY r.run_id""", run_ids).fetchall():
        runs[rid] = {
            "retrieved": retrieved, "extracted": extracted, "relevant": relevant,
            "aim": round(relevant / extracted, 3) if extracted else None,
            "screening": {"retrieved": retrieved, "extracted": extracted, "relevant": relevant,
                          "considered_window": min(cited_pool, 60)},
        }

    for rid, n_subs in conn.execute(
        f"SELECT run_id, count(*) FROM sub_questions WHERE run_id IN ({ph}) GROUP BY run_id",
        run_ids).fetchall():
        runs.setdefault(rid, {})["subs"] = n_subs

    # findings: specificity over accepted, cited evidence ids, dispositions for the ledger
    for rid in run_ids:
        rows = conn.execute(
            "SELECT claim, label, disposition, evidence_ids FROM findings WHERE run_id=%s",
            (rid,)).fetchall()
        accepted = [r for r in rows if r[2] == "accepted"]
        cited: set[int] = set()
        for _, _, _, ev in accepted:
            cited.update(ev or [])
        by_disp: dict[str, int] = {}
        for _, _, disp, _ in rows:
            by_disp[disp or "none"] = by_disp.get(disp or "none", 0) + 1
        r = runs.setdefault(rid, {})
        r["findings"] = len(rows)
        r["accepted"] = len(accepted)
        r["unknowns"] = sum(1 for x in accepted if x[1] == "unknown")
        r["specific"] = sum(1 for x in accepted if is_specific(x[0]))
        r["specificity"] = round(r["specific"] / len(accepted), 3) if accepted else None
        r.setdefault("screening", {})["cited"] = len(cited)
        r["screening"]["dispositions"] = by_disp

    # ── repair rate (gap 6) ───────────────────────────────────────────────────────────────────
    # DENOMINATOR IS THE REVIEWS TABLE, not dispositions. The first version counted findings whose
    # disposition was 'rejected_by_reviewer' or 'superseded_by_revision', and its numerator wanted
    # revision_round>0 AND disposition='accepted' — so a revision that succeeded and was LATER
    # superseded by a follow-up round's re-synthesis vanished from the numerator while its original
    # stayed in the denominator. Run 43 reported repair_rate 0.0 for a run whose own notes said
    # "1 rejected -> 1 revised". Dispositions are mutable; a reject review is a historical fact.
    #
    # Two numbers, because they answer different questions:
    #   repairs_attempted  — did the loop produce a replacement at all (the loop working)
    #   repairs_surviving  — did that replacement survive re-review AND any later round (the loop
    #                        helping). Reported separately so a low survival rate can never be
    #                        mistaken for the loop never firing.
    repair = _maybe(conn, f"""
        SELECT (SELECT count(DISTINCT f.parent_finding_id) FROM findings f
                WHERE f.run_id IN ({ph}) AND f.revision_round > 0),
               (SELECT count(*) FROM findings f
                WHERE f.run_id IN ({ph}) AND f.revision_round > 0
                AND f.disposition = 'accepted'),
               (SELECT count(DISTINCT r.finding_id) FROM reviews r
                WHERE r.run_id IN ({ph}) AND r.severity = 'reject')
    """, run_ids * 3)

    # ── gap closure (gap 7) ──────────────────────────────────────────────────────────────────
    # A COUNT PROXY, and labelled as one. The first version asked "does this run have any accepted
    # finding" per round-1 sub-question — which is true whenever the run produced anything at all,
    # so it scored 1.0 on run 43 while that run's own notes recorded "unknowns 3->3", i.e. nothing
    # closed. It measured that iteration HAPPENED, and reported it as iteration having WORKED.
    #
    # What is honestly derivable: unknown findings that a later round superseded (the gaps a round
    # inherited) versus unknown findings still live (the gaps that survived). Semantic matching of
    # WHICH unknown got answered is a judgement call this instrument does not make — the same limit
    # pipeline/followup.closure() documents for itself.
    closure = _maybe(conn, f"""
        SELECT count(*) FILTER (WHERE label='unknown'
                                AND disposition = 'superseded_by_revision'),
               count(*) FILTER (WHERE label='unknown'
                                AND COALESCE(disposition,'') <> 'superseded_by_revision')
        FROM findings WHERE run_id IN ({ph})
    """, run_ids)
    # Rounds actually run — separates "iteration never fired" from "iteration fired and closed
    # nothing". Without it a 0.0 closure is unreadable.
    rounds = _maybe(conn, f"SELECT count(*) FROM sub_questions "
                          f"WHERE run_id IN ({ph}) AND round > 0", run_ids)

    per_run = {str(k): v for k, v in sorted(runs.items())}
    extracted_live = sum(1 for v in runs.values() if v.get("extracted"))
    aims = [v["aim"] for v in runs.values() if v.get("aim") is not None]
    specs = [v["specificity"] for v in runs.values() if v.get("specificity") is not None]

    attempted = surviving = rejected = None
    if repair:
        attempted, surviving, rejected = repair[0]
    gaps_inherited = gaps_open = None
    if closure:
        gaps_inherited, gaps_open = closure[0]

    summary = {
        "n_runs": len(runs),
        "extraction_liveness": round(extracted_live / len(runs), 3) if runs else None,
        "mean_aim": round(sum(aims) / len(aims), 3) if aims else None,
        "mean_specificity": round(sum(specs) / len(specs), 3) if specs else None,
        "subs_per_run": round(sum(v.get("subs", 0) for v in runs.values()) / len(runs), 2) if runs else None,
        # None (not 0.0) when nothing was ever rejected: no reject reviews means the metric has no
        # denominator, which is not the same as a loop that failed. Same discipline as `aim`.
        "repair_rate": (round(attempted / rejected, 3)
                        if rejected else (None if repair else None)),
        "repairs_attempted": attempted,
        "repairs_surviving": surviving,
        "rejects_reviewed": rejected,
        "followup_subquestions": rounds[0][0] if rounds else None,
        "gaps_inherited_by_later_round": gaps_inherited,
        "gaps_still_open": gaps_open,
        "gap_closure": (round(max(0.0, (gaps_inherited - gaps_open) / gaps_inherited), 3)
                        if gaps_inherited else None),
    }
    return {"summary": summary, "runs": per_run}


def brief_retention(conn, synthesis_id: int | None) -> dict | None:
    """Entities surviving from accepted findings into the consolidated brief (gap 5)."""
    row = None
    if synthesis_id is not None:
        row = conn.execute("SELECT synthesis_id, run_ids, report_md FROM cross_syntheses "
                           "WHERE synthesis_id=%s", (synthesis_id,)).fetchone()
    else:
        row = conn.execute("SELECT synthesis_id, run_ids, report_md FROM cross_syntheses "
                           "WHERE status='delivered' ORDER BY synthesis_id DESC LIMIT 1").fetchone()
    if not row or not row[2]:
        return None
    sid, run_ids, brief = row[0], [int(r) for r in row[1]], row[2]
    ph = ",".join(["%s"] * len(run_ids))
    source_entities: set[str] = set()
    for (claim,) in conn.execute(
            f"SELECT claim FROM findings WHERE run_id IN ({ph}) AND disposition='accepted'",
            run_ids).fetchall():
        source_entities |= named_entities(claim)
    brief_entities = named_entities(brief) & source_entities  # only credit entities findings held
    return {"synthesis_id": sid, "runs": run_ids,
            "finding_entities": len(source_entities), "brief_entities": len(brief_entities),
            "retention": round(len(brief_entities) / len(source_entities), 3) if source_entities else None,
            "brief_chars": len(brief)}


def _parse_runs(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        elif part:
            out.append(int(part))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", required=True, help="e.g. 29-42 or 3,14,29-42")
    ap.add_argument("--synthesis", type=int, default=None,
                    help="cross_syntheses id for brief_retention (default: latest delivered)")
    ap.add_argument("--out", help="write scorecard JSON here (freeze a baseline)")
    ap.add_argument("--baseline", help="compare against a frozen baseline JSON")
    a = ap.parse_args()

    with _connect() as conn:
        card = run_metrics(conn, _parse_runs(a.runs))
        card["brief_retention"] = brief_retention(conn, a.synthesis)

    if a.baseline:
        base = json.load(open(a.baseline, encoding="utf-8"))
        deltas = {}
        for key, now in card["summary"].items():
            before = base.get("summary", {}).get(key)
            if isinstance(now, (int, float)) and isinstance(before, (int, float)):
                deltas[key] = round(now - before, 3)
        card["vs_baseline"] = {"file": a.baseline, "deltas": deltas}

    text = json.dumps(card, indent=2, ensure_ascii=False)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"scorecard written to {a.out}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
