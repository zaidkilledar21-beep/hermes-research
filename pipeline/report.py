"""Build and deliver the cited research report.

Every finding is rendered with its honesty label and resolvable evidence citations
(URL + grade + trust tag). Delivery goes to Telegram if configured, else a markdown file.
"""
from __future__ import annotations
import os
import pathlib
import requests
import psycopg

DATABASE_URL = os.environ["DATABASE_URL"]
LABEL_ICON = {"observed": "[OBSERVED]", "inferred": "[INFERRED]", "unknown": "[GAP]",
              "community_signal": "[COMMUNITY]"}
TIER_LABEL = {"primary_authority": "authority", "reference": "reference",
              "independent_review": "review", "vendor_marketing": "vendor-claim",
              "community": "community", "general_web": "web", "user_supplied": "user-doc"}


def build(run_id: int) -> str:
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        q, cost = conn.execute(
            "SELECT question, cost_usd FROM research_runs WHERE run_id=%s", (run_id,)).fetchone()
        all_findings = conn.execute(
            "SELECT finding_id, claim, label, confidence, evidence_ids, contradicts, "
            "disposition, disposition_detail FROM findings "
            "WHERE run_id=%s ORDER BY array_length(evidence_ids,1) DESC NULLS LAST",
            (run_id,)).fetchall()
        # Contradiction links may point at a quarantined finding, so resolve claims across ALL of them.
        claim_by_fid = {r[0]: r[1] for r in all_findings}
        # Only accepted findings are reported as findings. The rest are listed separately rather than
        # (v3 Part E) — except superseded originals: they are LINEAGE, not withheld claims. Their
        # revision is delivered (or withheld) under its own finding_id; listing the original too
        # would state the same claim twice with contradictory statuses.
        # silently dropped — a quarantined finding is information about the run, not something to hide.
        findings = [f for f in all_findings if f[6] == "accepted"]
        quarantined = [f for f in all_findings
                       if f[6] not in ("accepted", "superseded_by_revision")]
        superseded = sum(1 for f in all_findings if f[6] == "superseded_by_revision")
        ev = dict((r[0], r) for r in conn.execute(
            "SELECT evidence_id, url, grade, trust_tag, source_id, credibility_tier "
            "FROM evidence_items WHERE run_id=%s", (run_id,)).fetchall())
        # reviewer verdicts keyed by finding_id
        reviews: dict[int, list] = {}
        for fid, reviewer, verdict, severity in conn.execute(
            "SELECT finding_id, reviewer, verdict, severity FROM reviews WHERE run_id=%s", (run_id,)):
            reviews.setdefault(fid, []).append((reviewer, verdict, severity))
        spent = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM agent_runs WHERE run_id=%s", (run_id,)).fetchone()[0]
        # v3 M2 — screening ledger counts (PRISMA-style flow accounting). All from existing
        # columns; the report just never STATED them, which made "20 findings" look like the
        # whole story when 292 items were retrieved and 272 were screened out.
        scr = conn.execute(
            "SELECT count(*), count(extracted), "
            "count(*) FILTER (WHERE answers_question IS TRUE), "
            "count(*) FILTER (WHERE answers_question IS FALSE) "
            "FROM evidence_items WHERE run_id=%s", (run_id,)).fetchone()

    lines = [f"# Research report — run {run_id}", "", f"**Question:** {q}", ""]
    # v3 M2 — screening ledger: how the evidence funnel narrowed, with exclusions accounted for.
    # An expert review states its flow ("292 identified -> 60 screened -> 20 cited"); so does this.
    retrieved, extracted, relevant, irrelevant = scr
    cited_ids = {e for f in findings for e in (f[4] or [])}
    lines += ["## Screening",
              f"_retrieved {retrieved} -> extracted {extracted} -> judged relevant {relevant} "
              f"(irrelevant {irrelevant}, unjudged {retrieved - relevant - irrelevant}) -> "
              f"cited {len(cited_ids)}"
              + (f" · {len(quarantined)} finding(s) withheld by the gate" if quarantined else "")
              + "_", ""]
    observed = [f for f in findings if f[2] == "observed"]
    inferred = [f for f in findings if f[2] == "inferred"]
    community = [f for f in findings if f[2] == "community_signal"]
    gaps = [f for f in findings if f[2] == "unknown"]

    def render(title, group):
        if not group:
            return
        lines.append(f"## {title}")
        for fid, claim, label, conf, ev_ids, contradicts, _disp, _detail in group:
            tag = LABEL_ICON.get(label, label)
            c = f" (confidence {conf})" if conf is not None else ""
            lines.append(f"- {tag}{c} {claim}")
            cites = []
            for e in ev_ids or []:
                if e in ev:
                    _, url, grade, trust, src, tier = ev[e]
                    flag = " ⚠untrusted" if trust == "UNTRUSTED_EVIDENCE" else ""
                    t = TIER_LABEL.get(tier, tier or "web")
                    cites.append(f"[{src} · {t} · grade {grade}{flag}]({url or 'no-url'})")
            if cites:
                lines.append(f"  - evidence: {', '.join(cites)}")
            for cfid in contradicts or []:
                other = claim_by_fid.get(cfid)
                if other:
                    snippet = other if len(other) <= 140 else other[:137] + "..."
                    lines.append(f"  - ⚔ conflicts with: {snippet}")
            revs = reviews.get(fid, [])
            if revs:
                rtxt = ", ".join(f"{r[0]}: {r[1]}" for r in revs)
                lines.append(f"  - review: {rtxt}")
        lines.append("")

    render("Observed (directly supported)", observed)
    render("Inferred (reasoned from evidence)", inferred)
    render("Community signal (anecdotal / low-N, real but unverified)", community)
    render("Gaps (evidence did not answer)", gaps)

    if not findings:
        lines.append("## Insufficient evidence")
        lines.append("The analyst returned no deliverable findings for this question. This is an "
                     "honest negative result, not a failure — the retrieved evidence did not "
                     "support any claim worth reporting.")
        lines.append("")

    # Transparency: show what was withheld and why, rather than silently dropping it. A quarantined
    # finding is a fact about the run (model fabricated a citation, reviewer rejected a claim).
    if quarantined:
        lines.append(f"## Withheld findings ({len(quarantined)})")
        lines.append("_Not delivered as findings. Listed so nothing is silently dropped._")
        for fid, claim, label, _c, _e, _x, disp, detail in quarantined:
            snippet = claim if len(claim) <= 160 else claim[:157] + "..."
            lines.append(f"- [{disp}] ({label}) {snippet}")
            if detail:
                lines.append(f"  - reason: {detail}")
        lines.append("")

    lines.append("---")
    lines.append(f"_{len(ev)} evidence items · {len(findings)} findings delivered"
                 + (f" · {len(quarantined)} withheld" if quarantined else "")
                 + (f" · {superseded} revised after review" if superseded else "")
                 + f" · cost ${float(spent):.4f}_")
    lines.append("_Read-only research. Walled-source items flagged untrusted are scraped and unverified._")
    return "\n".join(lines)


def deliver(run_id: int, markdown: str | None = None, blocked: list[str] | None = None) -> None:
    if blocked:
        markdown = (f"# Research run {run_id} — BLOCKED by release gate\n\n"
                    + "\n".join(f"- {p}" for p in blocked)
                    + "\n\nNo report delivered; integrity checks failed.")
    # Always persist the report to the DB so any interface (web UI, dashboard) can read it.
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute("UPDATE research_runs SET report_md=%s WHERE run_id=%s", (markdown, run_id))
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_OWNER_ID")
    if token and chat:
        # Telegram caps at 4096 chars; chunk.
        for i in range(0, len(markdown), 3800):
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": markdown[i:i+3800],
                                "parse_mode": "Markdown", "disable_web_page_preview": True},
                          timeout=20)
    else:
        # report_md in the DB (above) is the real delivery; the file copy is best-effort and
        # must never crash the run if the dir isn't writable (e.g. running outside the container).
        try:
            out = pathlib.Path(os.environ.get("EVIDENCE_DIR", "/app/evidence")) / f"report-{run_id}.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(markdown, encoding="utf-8")
            print(f"[deliver] wrote {out}")
        except OSError as e:
            print(f"[deliver] report saved to DB; file copy skipped ({type(e).__name__})")
