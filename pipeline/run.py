"""Orchestrate one research run end to end.

  submit.py creates a research_runs row (status=decomposing) with a source plan.
  run.py picks it up: collect (legit + reach) -> synthesize -> release gate -> deliver.

Usage:  python -m pipeline.run --run <run_id>
Delivery: Telegram if TELEGRAM_BOT_TOKEN set, else writes /app/evidence/report-<run>.md.
"""
from __future__ import annotations
import argparse
import json
import math
import os
import subprocess
import sys
import time
import psycopg
from collectors import legit, search
from pipeline import (synthesize, release_gate, reach_bridge, report, reviewers, queries, extract,
                      select, registry, plan_queries, revise, followup, figures, priors)

DATABASE_URL = os.environ["DATABASE_URL"]
LEGIT = {"x", "github", "youtube", "rss", "web", "hackernews", "web_search",
         "sec_edgar", "courtlistener", "fda_enforcement"}  # v3 Part H primary sources
WALLED = {"reddit_reach", "reddit_threads", "instagram_reach", "facebook_reach",
          "stackexchange_reach", "trustpilot_reach", "forum_reach"}
# Sources whose queries are POOLED: every query variant contributes candidates, then one diverse
# subset is read. Keeping this separate from LEGIT/WALLED matters because failure-language families
# multiply the query count — without pooling, sharper aim would just multiply the read budget.
POOLED = {"web_search", "reddit_threads"}
# Per-source collection ceiling. Higher than before because free Nemotron extraction downstream
# condenses raw text, so a bigger raw haul no longer bloats the paid synthesis context.
COLLECT_LIMIT = int(os.environ.get("COLLECT_LIMIT", "40"))
# v3 Part K: how many forum-shaped web_search hits per sub-question go to the browser reader.
FORUM_REACH_CAP = int(os.environ.get("FORUM_REACH_CAP", "4"))
# Discovery + read budget (DISCOVER_PER_QUERY / WEB_READS / REDDIT_THREADS / per-venue caps) lives
# in pipeline/select.py — it is selection policy, and the eval harness scores the same constants
# production runs on. Referenced as select.X below.
# Reach completion wait. Thread reading is browser-rendered and slow (~30-60s per thread), so this
# has to be generous — but it exits as soon as every request id reports in, so it rarely waits long.
REACH_INGEST_INTERVAL = int(os.environ.get("REACH_INGEST_INTERVAL", "20"))
REACH_MAX_WAIT = int(os.environ.get("REACH_MAX_WAIT", "900"))


def _set_status(run_id: int, status: str) -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute("UPDATE research_runs SET status=%s WHERE run_id=%s", (status, run_id))


def _append_note(run_id: int, note: str) -> None:
    """Append one line to research_runs.notes, fail-soft. %s::text because concat_ws is variadic
    and Postgres cannot infer the parameter type (see the throttle-note comment below — the note
    recording a failure was itself silently dropped without the cast)."""
    try:
        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            conn.execute("UPDATE research_runs SET notes = concat_ws(E'\\n', notes, %s::text) "
                         "WHERE run_id=%s", (note, run_id))
    except Exception as e:
        print(f"[notes] could not record note: {type(e).__name__}: {e}", file=sys.stderr)


class _Budget:
    """Run-wide ceilings on the expensive work, not just per-sub-question ceilings.

    A decomposed run can carry many sub-questions, and each one used to get its own full discovery
    and read allowance — so the cost of a run scaled with however many facets the director invented.
    On a 4GB box with 30-60s browser renders that is the difference between a run and an outage.
    """

    def __init__(self, scale: float = 1.0) -> None:
        # scale < 1 = a follow-up round's reduced slice (v3 Part F): each deepening round gets a
        # fraction of the run ceilings, halved again per round, so iteration can never cost more
        # than the round-0 collection it refines.
        self.searches = max(1, int(select.int_env("MAX_SEARCHES_PER_RUN", 40) * scale))
        self.web_reads = max(1, int(select.int_env("MAX_WEB_READS_PER_RUN", 24) * scale))
        self.threads = max(1, int(select.int_env("MAX_THREADS_PER_RUN", 16) * scale))

    def spend(self, field: str, want: int) -> int:
        """Grant up to `want` units of a budget line, returning what is actually affordable."""
        left = max(0, getattr(self, field))
        grant = min(want, left)
        setattr(self, field, left - grant)
        if grant < want:
            print(f"[budget] {field}: granted {grant} of {want} requested (run ceiling reached)",
                  file=sys.stderr)
        return grant

    def refund(self, field: str, unused: int) -> None:
        """Return budget that was reserved but never spent.

        Reads are reserved BEFORE selection (the cap is an input to selection), so a facet that
        discovers nothing would otherwise still be charged its full allowance and starve the later
        sub-questions of reads that never happened.
        """
        if unused > 0:
            setattr(self, field, getattr(self, field) + unused)


def _discover_tiers(qs: list[str], budget: _Budget, *, site: str | None = None,
                    path_must_contain: str | None = None) -> list[list[str]]:
    """Discover each query variant into its OWN candidate pool. Fail-soft per query.

    Pools are kept separate rather than concatenated so the caller can give the base topic query a
    reserved share of the read budget (select.select_tiered) instead of letting whichever query ran
    first take everything.
    """
    pools: list[list[str]] = []
    for q in qs:
        if not budget.spend("searches", 1):
            break
        try:
            pools.append(legit.discover_urls(q, site=site, limit=select.DISCOVER_PER_QUERY,
                                             path_must_contain=path_must_contain))
        except Exception as e:
            print(f"[discover] '{q}' failed: {type(e).__name__}: {e}", file=sys.stderr)
            pools.append([])
    return pools


def _collect_web_search(run_id: int, base: str, question: str, budget: _Budget,
                        plan: dict | None = None) -> tuple[int, list[str]]:
    """Open-web collection: many aimed queries in, one diverse read set out.

    The failure-language families make discovery hit the complaint pages a topic query never
    surfaces; the tiered diversity pass then stops those extra queries from spending the whole read
    budget on whichever host ranks for all of them — and reserves half the budget for the base topic
    query, so a question that was never about failure keeps its authoritative sources.

    `plan` (v3): a validated planner plan replaces the deterministic expansions with model-aimed
    queries; None keeps this function byte-identical to v2 (plan_queries.to_variants falls through
    to queries.variants).
    """
    qs = plan_queries.to_variants(plan, "web_search", base)
    pools = _discover_tiers(qs, budget)
    if not pools:
        return 0
    cap = budget.spend("web_reads", select.WEB_READS)
    priority = registry.preferred(question, "site")
    urls = select.select_tiered([pools[0], select.interleave(pools[1:])],
                                total_cap=cap, per_key_cap=select.WEB_PER_DOMAIN,
                                key=select.domain_key, priority=priority,
                                floors=[math.ceil(cap * select.BASE_TIER_FLOOR)])
    budget.refund("web_reads", cap - len(urls))
    print(f"[collect:web_search] {len(qs)} queries -> {sum(len(p) for p in pools)} candidates -> "
          f"{len(urls)} reads across {len({select.domain_key(u) for u in urls})} domains"
          + (f" (registry priority: {priority})" if priority else ""), file=sys.stderr)
    # v3 Part K: forum-thread URLs route to the browser reader (forum_reach) — niche vertical
    # boards often reject a plain fetch that a real browser sails through, and those threads are
    # exactly the operator testimony this engine exists to find. Capped: each render is 30-60s on
    # a 4GB box, so a forum-heavy result set must not convert the whole read budget into renders.
    forum_urls = [u for u in urls if select.forum_shaped(u)][:FORUM_REACH_CAP]
    plain_urls = [u for u in urls if u not in set(forum_urls)]
    rids: list[str] = []
    for furl in forum_urls:
        try:
            rids.append(reach_bridge.request_reach(run_id, "forum_reach", furl, COLLECT_LIMIT))
        except Exception as e:
            print(f"[collect:forum_reach] {type(e).__name__}: {e}", file=sys.stderr)
            plain_urls.append(furl)   # fail-soft: fall back to the plain reader
    if forum_urls:
        print(f"[collect:web_search] routed {len(rids)} forum thread(s) to the browser reader",
              file=sys.stderr)
    return legit.read_urls(run_id, plain_urls), rids


def _collect_reddit_threads(run_id: int, base: str, question: str, budget: _Budget,
                            plan: dict | None = None) -> str | None:
    """Community collection: discover threads via the search engine, read them in ONE reach batch.

    Reddit's own search is retired (it returned anime/AITAH for niche B2B queries). Registry-known
    subreddits get their own site-scoped query AND jump the selection order, which is how a niche
    venue proven on past runs beats whatever generic subreddit ranks today.
    """
    qs = plan_queries.to_variants(plan, "reddit_threads", base)
    pools = _discover_tiers(qs, budget, site="reddit.com", path_must_contain="/comments/")
    known = registry.preferred(question, "subreddit")
    scoped: list[list[str]] = []
    for sub in known:
        scoped.extend(_discover_tiers([base], budget, site=f"reddit.com/{sub}",
                                      path_must_contain="/comments/"))
    if not pools and not scoped:
        return None
    cap = budget.spend("threads", select.REDDIT_THREADS)
    # THREE tiers, because registry-scoped pools are neither base nor expansion. They run the base
    # query restricted to a venue that answered this topic before, so filing them under expansion
    # let the reserved base floor exclude exactly the venue the registry exists to promote — but
    # merging them INTO the base tier was worse: two preferred subreddits could then take every
    # slot the base floor was protecting. Their own small floor gets the registry read without
    # letting past runs decide the whole present one.
    urls = select.select_tiered([pools[0] if pools else [],
                                 select.interleave(scoped),
                                 select.interleave(pools[1:])],
                                total_cap=cap, per_key_cap=select.REDDIT_PER_SUB,
                                key=select.reddit_key, priority=known,
                                floors=[math.ceil(cap * select.BASE_TIER_FLOOR),
                                        select.REGISTRY_TIER_FLOOR])
    budget.refund("threads", cap - len(urls))
    print(f"[collect:reddit_threads] {len(qs)} queries -> {sum(len(p) for p in pools)} candidates "
          f"-> {len(urls)} threads across {len({select.reddit_key(u) for u in urls})} subreddits"
          + (f" (registry priority: {known})" if known else ""), file=sys.stderr)
    if not urls:
        print("[collect:reddit_threads] no threads discovered", file=sys.stderr)
        return None
    # One request for the whole batch so the container uses a single browser session.
    return reach_bridge.request_reach(run_id, "reddit_threads", base, COLLECT_LIMIT, urls=urls)


def _collect_for_subs(run_id: int, question: str, subs: list[tuple], budget: _Budget
                      ) -> tuple[list[str], list[str]]:
    """Collect for a batch of sub-questions: plan (v3), then fan out per source.

    Factored out of process() so follow-up rounds (v3 Part F) reuse the ENTIRE collection path —
    planner included — on their reduced budget instead of reimplementing it. Returns
    (reach_request_ids, planner_states)."""
    reach_rids: list[str] = []
    plan_states: list[str] = []
    for sub_id, text, plan in subs:
        # A source listed twice in one plan used to get its whole allowance twice.
        plan = list(dict.fromkeys(plan or []))
        # v3 query planner: one bounded model call per sub-question, only when a pooled
        # search-engine source will consume it. EVERY failure path falls back to the deterministic
        # queries.variants() and records why (sub_questions.plan_state + the notes rollup).
        sub_plan: dict | None = None
        if (sub_id is not None and plan_queries.PLANNER_ENABLED
                and any(s in plan_queries.PLANNABLE_SOURCES for s in plan)):
            from collectors import common
            if common.budget_spent(run_id) >= plan_queries.CAP:
                plan_queries.persist_plan(run_id, sub_id, "fallback_budget_cap", None, "",
                                          plan_queries.PLANNER_MODEL)
                plan_states.append("fallback_budget_cap")
            else:
                try:
                    sub_plan, tele = plan_queries.plan_sub_question(question, text, plan)
                    plan_queries.persist_plan(run_id, sub_id, tele["state"], sub_plan,
                                              tele.get("raw", ""), tele["model"])
                    common.log_agent_run(run_id, "analyst", tele["model"],
                                         tele.get("tokens_in", 0), tele.get("tokens_out", 0),
                                         tele.get("cost", 0.0), skill="query-plan")
                    plan_states.append(tele["state"])
                except Exception as e:
                    print(f"[planner] {type(e).__name__}: {e}", file=sys.stderr)
                    plan_states.append("fallback_error")
        elif sub_id is not None and any(s in plan_queries.PLANNABLE_SOURCES for s in plan):
            plan_queries.persist_plan(run_id, sub_id, "fallback_disabled", None, "",
                                      plan_queries.PLANNER_MODEL)
            plan_states.append("fallback_disabled")
        # Search-syntax APIs (X/GitHub/HN/Reddit/SE/Instagram) parse the query as structured
        # search, not a sentence — a full research question reads as broken boolean syntax and
        # hard-fails (e.g. X: "Ambiguous use of and as a keyword"). Compress ONCE per sub-question,
        # not per source. URL/domain-shaped sources (web, rss, youtube, trustpilot_reach,
        # forum_reach) keep the raw text untouched — compressing a URL would break it.
        search_query = (queries.compress_for_search(text)
                        if any(s in queries.SEARCH_TYPE for s in plan) else text)
        for src in plan:
            # trustpilot_reach needs a BARE DOMAIN, not the question. Extract it; if the question
            # names no domain, skip the source (it can't do anything useful without one) rather
            # than handing it a sentence that just returns "No results".
            if src == "trustpilot_reach":
                domain = queries.extract_domain(text)
                if not domain:
                    print(f"[collect:{src}] skipped — no domain in question", file=sys.stderr)
                    continue
                try:
                    reach_rids.append(reach_bridge.request_reach(run_id, src, domain, COLLECT_LIMIT))
                except Exception as e:
                    print(f"[collect:{src}] {type(e).__name__}: {e}", file=sys.stderr)
                continue
            base = search_query if src in queries.SEARCH_TYPE else text
            # X ANDs every term: an 8-keyword compression matches ~nothing in its search window
            # (2 items across the whole 14-run campaign). With a plan, use its best short anchored
            # query; without one, truncate — 4 terms is the measured sweet spot (Part A5).
            if src == "x":
                base = plan_queries.short_query(sub_plan, base)
            # Pooled sources fan out across query variants at DISCOVERY, then read one diverse
            # selection — so aiming harder costs search calls, not read budget.
            if src in POOLED:
                try:
                    if src == "web_search":
                        _, forum_rids = _collect_web_search(run_id, base, question, budget,
                                                            plan=sub_plan)
                        reach_rids.extend(forum_rids)
                    else:
                        rid = _collect_reddit_threads(run_id, base, question, budget, plan=sub_plan)
                        if rid:
                            reach_rids.append(rid)
                except Exception as e:
                    print(f"[collect:{src}] {type(e).__name__}: {e}", file=sys.stderr)
                continue
            # Anecdote-mining: some community sources get an extra experience-focused phrasing.
            for q in queries.variants(src, base):
                try:
                    if src in LEGIT:
                        legit.DISPATCH[src](run_id, q, COLLECT_LIMIT)
                    elif src in WALLED:
                        reach_rids.append(reach_bridge.request_reach(run_id, src, q, COLLECT_LIMIT))
                except Exception as e:
                    print(f"[collect:{src}] {type(e).__name__}: {e}", file=sys.stderr)
    return reach_rids, plan_states


def _await_reach(run_id: int, reach_rids: list[str]) -> None:
    """Wait for the reach container to FINISH, tracked by request id — not a fixed sleep. Reading
    several browser-rendered threads takes minutes; a fixed window silently orphaned a whole
    community-evidence batch on run 27 (results were written after the caller gave up)."""
    if not reach_rids:
        return
    deadline = time.time() + REACH_MAX_WAIT
    outstanding = set(reach_rids)
    while outstanding and time.time() < deadline:
        time.sleep(REACH_INGEST_INTERVAL)
        _, done = reach_bridge.ingest_reach_detailed(run_id)
        outstanding -= done
    reach_bridge.ingest_reach(run_id)  # final sweep for anything written in the last interval
    if outstanding:
        print(f"[reach] {len(outstanding)} request(s) did not finish within "
              f"{REACH_MAX_WAIT}s: {sorted(outstanding)}", file=sys.stderr)


def process(run_id: int) -> int:
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        row = conn.execute(
            "SELECT question FROM research_runs WHERE run_id=%s", (run_id,)).fetchone()
        if not row:
            print(f"run {run_id} not found", file=sys.stderr); return 1
        question = row[0]
        subs = conn.execute(
            "SELECT sub_id, text, source_plan FROM sub_questions WHERE run_id=%s",
            (run_id,)).fetchall()

    # Fallback plan if no sub-questions were pre-decomposed: search the question across search-type
    # sources only ("web" needs a page URL, not a question — don't put it here, see queries.py).
    # sub_id=None marks the undecomposed path: the planner is skipped for it (nothing to persist
    # against, and it is already a degraded path), and the scorecard can see it in notes.
    if not subs:
        subs = [(None, question, ["x", "github", "hackernews", "reddit_reach"])]
        _append_note(run_id, "undecomposed fallback: single sub-question")
    # v3 Part J visibility: submit.py seeds one row holding the WHOLE question; the director is
    # supposed to replace it during status='decomposing'. When that never happened (every one of
    # the 14 campaign runs), the run silently operated at 1/Nth of its intended coverage. Named in
    # notes so the scorecard's subs_per_run metric has a cause attached, not just a number.
    elif len(subs) == 1 and (subs[0][1] or "").strip() == (question or "").strip():
        _append_note(run_id, "undecomposed: seed sub-question only (director never decomposed)")

    _set_status(run_id, "collecting")
    budget = _Budget()           # run-wide ceilings, shared across every sub-question
    reach_rids, plan_states = _collect_for_subs(run_id, question, subs, budget)
    # Planner rollup — the fail-soft planner's positive signal, visible without a DB client
    # (lessons #26: a fail-soft feature can be completely dead and still look healthy).
    if plan_states:
        planned = sum(1 for s in plan_states if s == "planned")
        fallbacks = [s for s in plan_states if s != "planned"]
        summary = f"planner: {planned}/{len(plan_states)} planned"
        if fallbacks:
            summary += f", fallbacks: {', '.join(sorted(set(fallbacks)))}"
        _append_note(run_id, summary)

    # A throttled search returns 200 with an empty result list, exactly like a search that found
    # nothing — so without this the report would say "no evidence" when the truth is "the search
    # engines suspended us". Same failure class the synthesis layer already types explicitly
    # (synthesis_state): a failure must never be presentable as a negative result.
    if search.throttled_queries or search.degraded_queries:
        parts = []
        if search.throttled_queries:
            parts.append(f"{search.throttled_queries} discovery quer"
                         f"{'y' if search.throttled_queries == 1 else 'ies'} returned NOTHING while "
                         f"upstream search engines were unresponsive")
        if search.degraded_queries:
            parts.append(f"{search.degraded_queries} returned partial results with some engines "
                         f"unresponsive")
        # "unresponsive" rather than "rate-limited": suspension is the common cause but a timeout or
        # a misconfigured engine looks identical from here, and the note should not assert a cause
        # the evidence does not carry.
        note = ("; ".join(parts) +
                ". Coverage for this run is INCOMPLETE, not absent — absence of evidence here does "
                "not mean evidence of absence.")
        print(f"[collect] {note}", file=sys.stderr)
        # %s::text-cast concat_ws append — the cast matters, see _append_note's docstring (the note
        # recording that coverage was incomplete was itself silently dropped without it).
        _append_note(run_id, note)

    _await_reach(run_id, reach_rids)

    # Bulk extraction (free Nemotron): clean every raw item into dense, claim-preserving evidence
    # BEFORE synthesis, so the paid model spends its context on signal, not site chrome. Fail-soft —
    # any item that doesn't get extracted just falls back to its raw content in synthesis.
    _set_status(run_id, "extracting")
    extracted_ok = True
    try:
        extract.extract_run(run_id)
    except Exception as e:
        extracted_ok = False
        print(f"[extract] stage skipped: {type(e).__name__}: {e}", file=sys.stderr)

    # Learn where the answering evidence actually came from. Runs here (not earlier) because the
    # only honest measure of a venue is the extractor's relevance verdict, which exists only after
    # extraction — and is SKIPPED entirely if extraction failed, because the per-run ledger makes
    # the first write final: recording "0 useful" from a failed extraction would permanently teach
    # the registry that every venue in this run was worthless.
    if extracted_ok:
        try:
            venues = registry.record_run(run_id)
            if venues:
                print(f"[registry] recorded {venues} venues for run {run_id}", file=sys.stderr)
        except Exception as e:
            print(f"[registry] stage skipped: {type(e).__name__}: {e}", file=sys.stderr)
    else:
        print("[registry] skipped — extraction failed, relevance verdicts are not trustworthy",
              file=sys.stderr)

    _set_status(run_id, "synthesizing")
    synthesize.synthesize(run_id, question)
    # v3 Part G: conflicting figures are forced to meet each other BEFORE review, so the
    # conflict claim faces the same reviewers and gate as everything else. Deterministic, $0.
    try:
        figures.cross_check(run_id)
    except Exception as e:
        print(f"[figures] skipped: {type(e).__name__}: {e}", file=sys.stderr)
    # v3 Part L: figures that deviate >3x from this vertical's prior-run median get a surprise
    # flag — also a reviewable finding, never an auto-reject. Silent below MIN_N observations
    # (cold start is honest: no signal != nothing surprising).
    try:
        flags, thin = priors.check_run(run_id)
        if flags or thin:
            _append_note(run_id, f"priors: {flags} surprise flag(s), {thin} subject(s) below "
                                 f"MIN_N — insufficient priors, silent")
    except Exception as e:
        print(f"[priors] skipped: {type(e).__name__}: {e}", file=sys.stderr)

    # Optional bounded reviewers (Codex challenge + Claude judgment). Never crash the run on
    # reviewer failure — they inform the gate, they don't replace it.
    _set_status(run_id, "reviewing")
    try:
        reviewers.run_reviews(run_id)
    except Exception as e:
        print(f"[reviewers] skipped: {type(e).__name__}: {e}", file=sys.stderr)

    # v3 revision loop (Part E): rejected findings get ONE defend-or-revise pass instead of a
    # silent burial, then ONLY the revised rows go back to the reviewers. The gate below still
    # runs exactly once, over the final set — it remains the sole authority on delivery.
    # Fail-soft: any failure leaves every finding exactly as the gate would have disposed it.
    try:
        rev_counts = revise.revise_run(run_id, question)
        if rev_counts["rejected"]:
            _append_note(run_id,
                         f"revision: {rev_counts['rejected']} rejected -> "
                         f"{rev_counts['revised']} revised, {rev_counts['defended']} defended, "
                         f"{rev_counts['dropped']} dropped")
        if rev_counts["new_ids"]:
            reviewers.run_reviews(run_id, only_finding_ids=rev_counts["new_ids"])
    except Exception as e:
        print(f"[revise] skipped: {type(e).__name__}: {e}", file=sys.stderr)

    # v3 gap-driven iteration (Part F): the engine consumes the follow-ups it already writes.
    # Each round: harvest unknowns + unresolved contradictions -> plan <=N new sub-questions ->
    # collect on a reduced budget slice -> re-synthesize over ALL evidence -> re-review. The gate
    # still runs exactly once, after the loop settles. A round must close >=MIN_CLOSURE of the
    # previous round's unknowns to earn another; surviving unknowns stay honest unknowns.
    if followup.ENABLED:
        try:
            prev_unknowns: int | None = None
            for rnd in range(1, followup.ROUNDS + 1):
                gaps = followup.harvest_gaps(run_id)
                n_unknowns = len(gaps["unknowns"])
                if prev_unknowns is not None:
                    rate = followup.closure(prev_unknowns, n_unknowns)
                    if rate < followup.MIN_CLOSURE:
                        _append_note(run_id, f"followup round {rnd}: closure {rate} < "
                                             f"{followup.MIN_CLOSURE} — stopped")
                        break
                if not gaps["unknowns"] and not gaps["contradictions"]:
                    break
                from collectors import common
                if common.budget_spent(run_id) >= followup.CAP:
                    _append_note(run_id, f"followup round {rnd}: budget cap — skipped")
                    break
                new_subs = followup.plan_round(run_id, question, gaps, rnd)
                if not new_subs:
                    break
                _set_status(run_id, "deepening")
                ev_before = followup.evidence_count(run_id)
                round_budget = _Budget(scale=followup.BUDGET_FRACTION ** rnd)
                r_rids, _ = _collect_for_subs(run_id, question, new_subs, round_budget)
                _await_reach(run_id, r_rids)
                _set_status(run_id, "extracting")
                try:
                    extract.extract_run(run_id)
                except Exception as e:
                    print(f"[followup:extract] {type(e).__name__}: {e}", file=sys.stderr)
                followup.supersede_findings(run_id, rnd)
                _set_status(run_id, "synthesizing")
                synthesize.synthesize(run_id, question)
                try:
                    figures.cross_check(run_id)
                except Exception as e:
                    print(f"[figures] skipped: {type(e).__name__}: {e}", file=sys.stderr)
                _set_status(run_id, "reviewing")
                try:
                    reviewers.run_reviews(run_id)
                except Exception as e:
                    print(f"[followup:reviewers] {type(e).__name__}: {e}", file=sys.stderr)
                after = len(followup.harvest_gaps(run_id)["unknowns"])
                _append_note(run_id,
                             f"followup round {rnd}: {n_unknowns} unknowns + "
                             f"{len(gaps['contradictions'])} contradictions -> "
                             f"{len(new_subs)} subqs, +{followup.evidence_count(run_id) - ev_before}"
                             f" evidence, unknowns {n_unknowns}->{after}")
                prev_unknowns = n_unknowns
        except Exception as e:
            # Fail-soft: whatever finding set exists when the loop dies goes to the gate as-is.
            print(f"[followup] skipped: {type(e).__name__}: {e}", file=sys.stderr)

    # The gate now returns SYSTEMIC blockers only; finding-local problems are recorded as
    # per-finding dispositions and the rest of the report still ships. Entries prefixed "WARN:"
    # are advisory (e.g. budget overrun — operational, not epistemic) and must NOT block delivery:
    # suppressing an otherwise-valid report over a cost overrun loses real work for no integrity gain.
    gate_problems = release_gate.check(run_id)
    blockers = [p for p in gate_problems if not str(p).startswith("WARN:")]
    warnings = [p for p in gate_problems if str(p).startswith("WARN:")]
    if warnings:
        print(f"run {run_id} warnings: {warnings}", file=sys.stderr)
    if blockers:
        _set_status(run_id, "gated")
        report.deliver(run_id, blocked=blockers)
        print(f"run {run_id} gated: {blockers}", file=sys.stderr)
        return 2

    md = report.build(run_id)
    report.deliver(run_id, markdown=md)
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute("UPDATE research_runs SET status='delivered', delivered_at=now() WHERE run_id=%s",
                     (run_id,))
    # v3 Part L write path — AFTER the gate, accepted-only: quarantined figures must never teach
    # the vertical memory (the same first-write-is-final caution the venue registry learned).
    try:
        written = priors.record_run(run_id)
        if written:
            print(f"[priors] recorded {written} figure(s) into vertical memory", file=sys.stderr)
    except Exception as e:
        print(f"[priors] record skipped: {type(e).__name__}: {e}", file=sys.stderr)
    print(f"run {run_id} delivered")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, required=True)
    return process(ap.parse_args().run)


if __name__ == "__main__":
    raise SystemExit(main())
