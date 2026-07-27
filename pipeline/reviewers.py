"""Hermes-side bridge to the isolated reviewer container (Claude + Codex CLIs).

Per master-plan ADR-005 / §17.5: reviewers are OPTIONAL, BOUNDED, ISOLATED. They inform; they
never approve delivery (only release_gate.py, code, can). If the reviewer container / CLI auth is
unavailable, reviews are skipped gracefully — a run must never crash because a subscription CLI is down.

Flow (over a shared `review` volume, mirroring the reach pattern):
  - drop a sanitized packet per finding to review/req/  (claim + cited evidence text only)
  - reviewer container runs codex exec (evidence challenge) + claude -p (quality/overreach judgment)
  - ingest review/out/ into the `reviews` table
Hermes holds NO Claude/Codex credential; the reviewer container holds NO Neon/OpenRouter/platform secret.
"""
from __future__ import annotations
import argparse
import json
import os
import pathlib
import sys
import time
import psycopg

DATABASE_URL = os.environ["DATABASE_URL"]
# Pipeline runs on the host; the reviewer container shares this dir. Override with REVIEW_DIR.
REVIEW = pathlib.Path(os.environ.get("REVIEW_DIR", "/opt/review"))
REQ = REVIEW / "req"
OUT = REVIEW / "out"
# reviewers enabled only when explicitly turned on AND the shared dir exists (container deployed)
ENABLED = os.environ.get("REVIEWERS_ENABLED") == "1" and REVIEW.exists()


# Every ASSERTIVE label gets reviewed. community_signal was previously excluded, which was exactly
# backwards: it is the most fragile class (low-N, anonymous, UNTRUSTED scraped community text) and
# it is the engine's headline product, yet it received zero adversarial scrutiny while the safest
# class (observed) got double-reviewed. Only 'unknown' (an explicit gap) needs no review.
REVIEWED_LABELS = ("observed", "inferred", "community_signal")


def _packets(run_id: int, only_finding_ids: list[int] | None = None) -> list[dict]:
    """One sanitized packet per assertive finding. 'unknown' gaps need no review.

    `only_finding_ids` (v3 revision loop): re-review targets JUST the revised rows — re-reviewing
    the whole run would duplicate verdicts on settled findings and re-review superseded lineage.
    Revised rows carry their revision note (action + reason + defence quote) so a reviewer judging
    a DEFENDED claim sees the rebuttal it is asserting, not the bare claim it already rejected."""
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        # Superseded rows are lineage (v3 Parts E/F) — re-reviewing a claim that has already been
        # replaced wastes two CLI calls per row and can only produce verdicts on text nobody ships.
        if only_finding_ids:
            findings = conn.execute(
                "SELECT finding_id, claim, label, evidence_ids, disposition_detail FROM findings "
                "WHERE run_id=%s AND label = ANY(%s) AND finding_id = ANY(%s) "
                "AND COALESCE(disposition,'') <> 'superseded_by_revision'",
                (run_id, list(REVIEWED_LABELS), list(only_finding_ids))).fetchall()
        else:
            findings = conn.execute(
                "SELECT finding_id, claim, label, evidence_ids, NULL FROM findings "
                "WHERE run_id=%s AND label = ANY(%s) "
                "AND COALESCE(disposition,'') <> 'superseded_by_revision'",
                (run_id, list(REVIEWED_LABELS))).fetchall()
        # Reviewers must judge the SAME text synthesis reasoned over. synthesize.load_evidence uses
        # COALESCE(extracted, content); reading raw `content` here meant a reviewer could reject a
        # sound finding because it was reading boilerplate the analyst never saw.
        ev = {r[0]: r for r in conn.execute(
            "SELECT evidence_id, grade, trust_tag, COALESCE(extracted, content) "
            "FROM evidence_items WHERE run_id=%s", (run_id,)).fetchall()}
    packets = []
    for fid, claim, label, ev_ids, revision_note in findings:
        cited = []
        for e in ev_ids or []:
            if e in ev:
                _, grade, trust, content = ev[e]
                # send only what a reviewer needs; content already sanitized at store time.
                # Cap matches synthesize.MAX_EVIDENCE_CHARS so both stages see the same window.
                cited.append({"grade": grade, "trust": trust, "text": (content or "")[:2500]})
        packet = {"finding_id": fid, "claim": claim, "label": label, "evidence": cited}
        if revision_note:
            packet["revision_note"] = revision_note[:600]
        packets.append(packet)
    return packets


# The reviewer container processes packets SEQUENTIALLY and each packet costs two CLI calls
# (Codex + Claude). A fixed 90s wait therefore silently lost reviews as soon as a run produced more
# than a handful of findings: run 26 wrote 15 packets, the wait expired, and every verdict was
# orphaned in the dropbox (stale out/ files from runs 16-18 were the same failure, unnoticed).
# So: poll until the queue for this run drains, with a budget that scales with packet count.
PER_PACKET_SECONDS = int(os.environ.get("REVIEW_SECONDS_PER_PACKET", "45"))
REVIEW_POLL_INTERVAL = int(os.environ.get("REVIEW_POLL_INTERVAL", "15"))
REVIEW_MAX_WAIT = int(os.environ.get("REVIEW_MAX_WAIT", "900"))


def run_reviews(run_id: int, wait: int | None = None,
                only_finding_ids: list[int] | None = None) -> int:
    if not ENABLED:
        print("[reviewers] disabled (reviewer container not deployed) — skipping")
        return 0
    REQ.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    packets = _packets(run_id, only_finding_ids=only_finding_ids)
    if not packets:
        return 0
    for p in packets:
        p["run_id"] = run_id
        (REQ / f"{run_id}-{p['finding_id']}.json").write_text(json.dumps(p), encoding="utf-8")

    budget = wait if wait is not None else min(REVIEW_MAX_WAIT,
                                               60 + len(packets) * PER_PACKET_SECONDS)
    deadline = time.time() + budget
    ingested = 0
    while time.time() < deadline:
        time.sleep(REVIEW_POLL_INTERVAL)
        ingested += _ingest(run_id)
        if not list(REQ.glob(f"{run_id}-*.json")):
            # queue drained; one more sweep for verdicts written during this last interval
            time.sleep(REVIEW_POLL_INTERVAL)
            ingested += _ingest(run_id)
            break
    else:
        print(f"[reviewers] budget {budget}s expired for run {run_id}; "
              f"{len(list(REQ.glob(f'{run_id}-*.json')))} packets unreviewed", file=sys.stderr)
    print(f"[reviewers] ingested {ingested} verdicts for run {run_id}")
    return ingested


def _ingest(run_id: int) -> int:
    n = 0
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        for f in sorted(OUT.glob(f"{run_id}-*.json")):
            try:
                res = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                f.unlink(missing_ok=True); continue
            for rev in res.get("reviews", []):
                conn.execute(
                    "INSERT INTO reviews (run_id, finding_id, reviewer, severity, verdict, detail) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (run_id, res.get("finding_id"), rev.get("reviewer"),
                     rev.get("severity", "info"), rev.get("verdict", "unknown"),
                     rev.get("detail")))
                n += 1
            f.unlink(missing_ok=True)
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, required=True)
    ap.add_argument("--wait", type=int, default=90)
    a = ap.parse_args()
    print(f"ingested {run_reviews(a.run, a.wait)} reviews for run {a.run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
