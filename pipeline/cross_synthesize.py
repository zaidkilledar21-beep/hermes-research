"""Cross-run synthesizer — host side.

Consolidates several DELIVERED research runs into ONE intelligence brief. Assembles a findings
packet from Neon, drops it into the reviewer container's dropbox (kind="cross_synthesis"), and the
container runs the two-model synth+critic chain (Claude Opus draft -> Codex critique -> Claude
revise) — subscription-backed, $0. The container never touches Neon (same isolation as reviewers.py);
the packet text is DATA, never instructions.

Usage:  python -m pipeline.cross_synthesize --synthesis <id>
(the web app creates the cross_syntheses row first, then spawns this)
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
import pathlib
import psycopg

DATABASE_URL = os.environ["DATABASE_URL"]
# Same shared review dropbox reviewers.py uses (container mounts it at /app/review).
REVIEW = pathlib.Path(os.environ.get("REVIEW_DIR", "/opt/review"))
REQ = REVIEW / "req"
OUT = REVIEW / "out"
POLL_SECONDS = 10
MAX_WAIT = int(os.environ.get("SYNTH_MAX_WAIT", "600"))  # the 3-call chain is slow; allow ~10 min


def _set_status(sid: int, status: str) -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute("UPDATE cross_syntheses SET status=%s WHERE synthesis_id=%s", (status, sid))


def _assemble_packet(run_ids: list[int]) -> dict:
    """Pull each run's question + its findings (with cited-evidence provenance) into a compact packet.
    Findings-level only — evidence was already distilled at synthesis time; re-dumping raw text would
    bloat the brief's context for no gain."""
    runs = []
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        for rid in run_ids:
            row = conn.execute(
                "SELECT question, status FROM research_runs WHERE run_id=%s", (rid,)).fetchone()
            if not row or row[1] != "delivered":
                continue  # only consolidate runs that actually delivered
            ev = {r[0]: r for r in conn.execute(
                "SELECT evidence_id, source_id, url, credibility_tier, grade FROM evidence_items "
                "WHERE run_id=%s", (rid,)).fetchall()}
            # ONLY accepted findings. The release gate withholds findings that failed review, and
            # report.py lists them as withheld rather than stating them — but this packet used to
            # select every row regardless of disposition, so the consolidated brief silently
            # re-admitted them as fact. Observed live: a rejected "2025-26 enforcement wave" claim
            # (naming a warehouse raid) was withheld from run 31's report and then asserted, without
            # any marker, in the cross-run brief over runs 29-36. A gate that only holds at the
            # per-run layer is not a gate.
            withheld = conn.execute(
                "SELECT count(*) FROM findings WHERE run_id=%s AND disposition <> 'accepted'",
                (rid,)).fetchone()[0]
            findings = []
            for fid, claim, label, conf, ev_ids in conn.execute(
                "SELECT finding_id, claim, label, confidence, evidence_ids FROM findings "
                "WHERE run_id=%s AND disposition = 'accepted' ORDER BY label", (rid,)).fetchall():
                cites = [{"source": ev[e][1], "tier": ev[e][3], "grade": ev[e][4], "url": ev[e][2]}
                         for e in (ev_ids or []) if e in ev]
                # confidence is NUMERIC -> Decimal from psycopg; cast to float for JSON.
                findings.append({"claim": claim, "label": label,
                                 "confidence": float(conf) if conf is not None else None,
                                 "sources": cites})
            # Carried so the brief can be honest about coverage: these findings existed, were
            # judged unsupportable, and are deliberately absent from what follows.
            runs.append({"run_id": rid, "question": row[0], "findings": findings,
                         "withheld_findings": withheld})
    return {"runs": runs}


def synthesize(synthesis_id: int) -> int:
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        row = conn.execute(
            "SELECT run_ids, title FROM cross_syntheses WHERE synthesis_id=%s", (synthesis_id,)
        ).fetchone()
    if not row:
        print(f"synthesis {synthesis_id} not found", file=sys.stderr); return 1
    run_ids, title = list(row[0]), row[1]

    packet = _assemble_packet(run_ids)
    if not packet["runs"]:
        _deliver(synthesis_id, "No delivered runs to consolidate (all requested runs missing/gated).",
                 status="failed")
        return 2

    REQ.mkdir(parents=True, exist_ok=True); OUT.mkdir(parents=True, exist_ok=True)
    name = f"synth-{synthesis_id}.json"
    (REQ / name).write_text(json.dumps(
        {"kind": "cross_synthesis", "synthesis_id": synthesis_id, "title": title,
         "findings": packet}), encoding="utf-8")

    waited = 0
    while waited < MAX_WAIT:
        outf = OUT / name
        if outf.exists():
            try:
                res = json.loads(outf.read_text(encoding="utf-8"))
            except Exception:
                res = {}
            outf.unlink(missing_ok=True)
            md = res.get("report_md") or ""
            if not md:
                _deliver(synthesis_id, f"Synthesis failed: {res.get('error','no output')}",
                         status="failed")
                return 2
            _deliver(synthesis_id, _compose(md, res.get("critique")), status="delivered")
            return 0
        time.sleep(POLL_SECONDS); waited += POLL_SECONDS

    _deliver(synthesis_id, "Synthesis timed out waiting for the reviewer container.", status="failed")
    return 2


def _compose(brief_md: str, critique: dict | None) -> str:
    """Final report = the revised brief + a transparency appendix of what the Codex critic challenged."""
    out = [brief_md.strip()]
    if critique:
        out += ["", "---", "## Adversarial review (Codex)"]
        for key, label in (("omissions", "Omissions flagged"), ("overreaches", "Overreaches flagged"),
                           ("missed_contradictions", "Missed contradictions"), ("invented", "Unsupported/invented")):
            items = critique.get(key) or []
            if items:
                out.append(f"**{label}:**")
                out += [f"- {x}" for x in items]
        if critique.get("overall"):
            out += ["", f"_Critic verdict: {critique['overall']}_"]
    return "\n".join(out)


def _deliver(sid: int, markdown: str, status: str) -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            "UPDATE cross_syntheses SET report_md=%s, status=%s, "
            "delivered_at=CASE WHEN %s='delivered' THEN now() ELSE delivered_at END "
            "WHERE synthesis_id=%s", (markdown, status, status, sid))
    print(f"synthesis {sid} -> {status}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthesis", type=int, required=True)
    return synthesize(ap.parse_args().synthesis)


if __name__ == "__main__":
    raise SystemExit(main())
