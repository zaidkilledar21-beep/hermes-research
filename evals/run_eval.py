"""Discovery-targeting eval runner — scores the engine's AIM without running research.

  python -m evals.run_eval --plan-only                 # offline: scores the query plan only
  python -m evals.run_eval                             # + live SearXNG discovery + selection
  python -m evals.run_eval --legacy-plan --plan-only   # what the pre-v2 engine would have planned
  python -m evals.run_eval --save-baseline evals/baseline.json
  python -m evals.run_eval --baseline evals/baseline.json

DELIBERATELY NOT A RESEARCH RUN. It never inserts a research_runs row, never reads a page, never
calls a paid model, never writes to the database. It stops exactly where the money starts: it plans
queries, asks SearXNG which URLs those queries surface, applies the real selection policy
(pipeline/select.py, the same constants production uses), and scores what WOULD have been read. That
is the whole bottleneck — run 28 retrieved plenty and aimed badly — and it is the part that can be
measured for free, so it is the part worth measuring on every change.

`--plan-only` needs nothing but this repo: no database, no SearXNG, no network. Full mode needs
SearXNG reachable and NOTHING else — discovery lives in collectors/search.py precisely so this
harness never has to import the evidence store, or a DATABASE_URL, to ask a search engine a question.

The vertical source registry is EXCLUDED by default (--use-registry opts in), and that IS a
deliberate divergence from production: the registry is mutable state that grows with every run, so
including it would mean two eval runs of identical code could score differently. A benchmark that
cannot be replayed is not a benchmark. Measure the registry's contribution as its own labelled
comparison, not as background noise in every other measurement.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

from collectors import search
from evals import score as scorer
from pipeline import queries, select

QUESTIONS = Path(__file__).with_name("questions.json")


def load_questions(path: Path = QUESTIONS) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["questions"]


def plan_for(spec: dict, legacy: bool = False,
             planner_plan: dict | None = None) -> dict[str, list[str]]:
    """The queries pipeline/run.py would issue for this question, per source.

    `legacy=True` models the PRE-failure-family engine: one topic-shaped query per source, no
    anchoring, no families. It exists so the eval can show what changed without checking out old
    code — a benchmark where the new behaviour scores 1.0 by construction proves nothing until you
    can see the number the old behaviour got.

    `planner_plan` (v3): a validated plan from pipeline/plan_queries. Routed through the SAME
    to_variants() production uses, so the eval measures the real integration path, not a parallel
    reimplementation of it.
    """
    compressed = queries.compress_for_search(spec["question"])
    plan: dict[str, list[str]] = {}
    for src in spec.get("sources", ["web_search"]):
        base = compressed if src in queries.SEARCH_TYPE else spec["question"]
        if legacy:
            plan[src] = [base]
        elif planner_plan is not None:
            from pipeline import plan_queries
            plan[src] = plan_queries.to_variants(planner_plan, src, base)
        else:
            plan[src] = queries.variants(src, base)
    return plan


def planner_plans(specs: list[dict], cache_path: Path, refresh: bool) -> dict[str, dict | None]:
    """One real planner call per question, cached to disk keyed (question id, model) so reruns are
    free. ~10 calls at ~$3e-05 each on a cache miss — the only part of this harness that costs
    anything, which is why it is opt-in (--planner) and cached."""
    from pipeline import plan_queries

    cache: dict[str, dict] = {}
    if cache_path.exists() and not refresh:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    out: dict[str, dict | None] = {}
    dirty = False
    for spec in specs:
        key = f"{spec['id']}::{plan_queries.PLANNER_MODEL}"
        if key in cache:
            out[spec["id"]] = cache[key].get("plan")
            continue
        plan, tele = plan_queries.plan_sub_question(
            spec["question"], spec["question"], spec.get("sources", ["web_search"]))
        print(f"[eval:planner] {spec['id']}: {tele['state']} "
              f"(${tele.get('cost', 0):.5f})", file=sys.stderr)
        cache[key] = {"plan": plan, "state": tele["state"], "cost": tele.get("cost", 0)}
        out[spec["id"]] = plan
        dirty = True
    if dirty:
        cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[eval:planner] cache updated: {cache_path}", file=sys.stderr)
    return out


def _registry_venues(spec: dict, src: str, use_registry: bool) -> list[str]:
    if not use_registry:
        return []
    from pipeline import registry

    return registry.preferred(spec["question"],
                              "subreddit" if src == "reddit_threads" else "site")


def discover_for(spec: dict, plan: dict[str, list[str]],
                 use_registry: bool = False) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Live discovery + the production selection policy. Returns (selected, base_tier_candidates).

    Mirrors pipeline/run.py exactly: one pool per query, base pool in its own tier with a reserved
    floor, registry venues discovered through the site= parameter (NOT by pasting `site:` into the
    query text, which double-prefixed and searched for `site:reddit.com site:reddit.com/r/x`).
    """
    selected: dict[str, list[str]] = {}
    base_pools: dict[str, list[str]] = {}
    for src, qs in plan.items():
        site = "reddit.com" if src == "reddit_threads" else None
        path = "/comments/" if src == "reddit_threads" else None
        pools: list[list[str]] = []
        for q in qs:
            try:
                pools.append(search.discover_urls(q, site=site, limit=select.DISCOVER_PER_QUERY,
                                                  path_must_contain=path))
            except Exception as e:
                print(f"[eval] discovery failed for '{q}': {type(e).__name__}: {e}",
                      file=sys.stderr)
                pools.append([])
        venues = _registry_venues(spec, src, use_registry)
        scoped_pools: list[list[str]] = []
        for venue in venues:
            scoped = f"reddit.com/{venue}" if src == "reddit_threads" else venue
            scoped_pools.append(search.discover_urls(qs[0], site=scoped,
                                                     limit=select.DISCOVER_PER_QUERY,
                                                     path_must_contain=path))
        if not pools and not scoped_pools:
            selected[src], base_pools[src] = [], []
            continue
        if src == "reddit_threads":
            cap, per_key, key = select.REDDIT_THREADS, select.REDDIT_PER_SUB, select.reddit_key
        else:
            cap, per_key, key = select.WEB_READS, select.WEB_PER_DOMAIN, select.domain_key
        # Same three tiers as pipeline/run.py: plain base query, registry-scoped, expansions.
        selected[src] = select.select_tiered(
            [pools[0] if pools else [], select.interleave(scoped_pools),
             select.interleave(pools[1:])],
            total_cap=cap, per_key_cap=per_key, key=key, priority=venues,
            floors=[math.ceil(cap * select.BASE_TIER_FLOOR), select.REGISTRY_TIER_FLOOR])
        # base_share must measure the PLAIN base query only. Counting registry-scoped hits as base
        # hits would have hidden the very displacement the metric exists to catch.
        base_pools[src] = pools[0] if pools else []
    return selected, base_pools


def evaluate(specs: list[dict], plan_only: bool, use_registry: bool,
             legacy: bool = False, plans: dict[str, dict | None] | None = None) -> list[dict]:
    rows: list[dict] = []
    for spec in specs:
        plan = plan_for(spec, legacy=legacy,
                        planner_plan=(plans or {}).get(spec["id"]))
        flat = [q for qs in plan.values() for q in qs]
        if plan_only:
            row = scorer.score_question(spec, flat, None)
            row.update(scorer.vocabulary_metrics(spec, flat))
            rows.append(row)
            continue
        selected, base_pools = discover_for(spec, plan, use_registry=use_registry)
        merged = [u for urls in selected.values() for u in urls]
        row = scorer.score_question(spec, flat, merged)
        row["per_source"] = {src: len(urls) for src, urls in selected.items()}
        # How much of the read budget the BASE topic query still holds. The control questions live
        # or die on this: complaint expansion is unconditional, so the guarantee that a regulatory
        # question keeps its primary sources is the reserved base-tier floor, not a gate on whether
        # the families fire.
        base_canon = {select.canonical(u) for urls in base_pools.values() for u in urls}
        row["base_share"] = (round(sum(1 for u in merged if select.canonical(u) in base_canon)
                                   / len(merged), 3) if merged else None)
        row["sample"] = merged[:5]
        rows.append(row)
    return rows


def summarize(rows: list[dict]) -> dict:
    scores = [r["score"] for r in rows if r.get("score") is not None]
    aims = [r["aim"] for r in rows if r.get("aim") is not None]
    discovered = [r for r in rows if r.get("selected") is not None]
    novelty = [r["novelty_rate"] for r in rows if r.get("novelty_rate") is not None]
    return {
        "questions": len(rows),
        "mean_score": round(statistics.fmean(scores), 3) if scores else None,
        "mean_aim": round(statistics.fmean(aims), 3) if aims else None,
        # v3, informational: how much vocabulary the plan added beyond the question's own words.
        "mean_novelty": round(statistics.fmean(novelty), 3) if novelty else None,
        "worst": min(((r["score"], r["id"]) for r in rows if r.get("score") is not None),
                     default=(None, None))[1],
        # A question that discovered NOTHING is not a question the engine aimed badly at — it is a
        # question the engine never got an answer for. Counted separately so the two can never be
        # averaged into one misleading number.
        "empty_questions": sum(1 for r in discovered if not r["selected"]),
        "throttled_queries": search.throttled_queries,
        "degraded_queries": search.degraded_queries,
    }


def usable_as_baseline(summary: dict) -> tuple[bool, str]:
    """Would freezing this report as a baseline mislead a future comparison?

    It would, badly, if the run was throttled: nine of ten questions scoring identically because
    discovery returned nothing is not a measurement of aim, and any later run compared against it
    would show a large fake improvement. Refuse rather than warn — a warning in a scroll-back is
    exactly how a poisoned baseline survives.
    """
    if summary.get("throttled_queries"):
        return False, (f"{summary['throttled_queries']} queries were throttled by upstream search "
                       f"engines — this run measured rate limits, not targeting")
    if summary.get("degraded_queries"):
        return False, (f"{summary['degraded_queries']} queries returned partial results with "
                       f"engines unresponsive — the result set is missing whatever those "
                       f"engines would have contributed")
    empty = summary.get("empty_questions") or 0
    if empty and empty >= max(1, summary["questions"] // 3):
        return False, (f"{empty}/{summary['questions']} questions discovered nothing — that is a "
                       f"search-availability problem, not a comparable result")
    return True, ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plan-only", action="store_true",
                    help="score the query plan offline (no SearXNG, no DB, no network)")
    ap.add_argument("--use-registry", action="store_true",
                    help="include vertical-registry venues (makes results non-replayable)")
    ap.add_argument("--legacy-plan", action="store_true",
                    help="model the pre-failure-family engine: one topic query per source")
    ap.add_argument("--planner", action="store_true",
                    help="v3: plan with the real LLM planner (one call per question, cached; "
                         "the only mode that spends money — cents on a cold cache)")
    ap.add_argument("--planner-cache", type=Path,
                    default=Path(__file__).with_name("planner_cache.json"))
    ap.add_argument("--planner-refresh", action="store_true",
                    help="ignore the cache and re-call the model for every question")
    ap.add_argument("--only", help="run a single question id")
    ap.add_argument("--questions", type=Path, default=QUESTIONS)
    ap.add_argument("--out", type=Path, help="write the full JSON report here")
    ap.add_argument("--baseline", type=Path, help="compare against a saved report")
    ap.add_argument("--save-baseline", type=Path, help="write this report as the new baseline")
    ap.add_argument("--force-baseline", action="store_true",
                    help="save a baseline even from a throttled/empty run (rarely right)")
    a = ap.parse_args()

    if a.planner and a.legacy_plan:
        print("--planner and --legacy-plan are mutually exclusive", file=sys.stderr)
        return 1
    specs = load_questions(a.questions)
    if a.only:
        specs = [s for s in specs if s.get("id") == a.only]
        if not specs:
            print(f"no question with id {a.only!r}", file=sys.stderr)
            return 1
    plans = planner_plans(specs, a.planner_cache, a.planner_refresh) if a.planner else None
    if not a.plan_only and not search.reachable():
        # Probes the CONFIGURED endpoint, so a wrong SEARXNG_URL degrades immediately instead of
        # burning one full HTTP timeout per query and then reporting a confident zero.
        print(f"[eval] SearXNG not reachable at {search.base_url()} — falling back to --plan-only",
              file=sys.stderr)
        a.plan_only = True

    rows = evaluate(specs, plan_only=a.plan_only, use_registry=a.use_registry,
                    legacy=a.legacy_plan, plans=plans)
    report = {"mode": ("plan-only" if a.plan_only else "discovery")
                      + (" (legacy plan)" if a.legacy_plan else "")
                      + (" (llm planner)" if a.planner else "")
                      + (" +registry" if a.use_registry else ""),
              "summary": summarize(rows), "rows": rows}
    print(json.dumps(report["summary"], indent=2))
    if report["summary"].get("throttled_queries"):
        print(f"[eval] WARNING: {report['summary']['throttled_queries']} queries were "
              f"throttled upstream — scores below understate the engine, they do not "
              f"measure it.", file=sys.stderr)
    for row in rows:
        print(f"  {row['id']:<30} aim={row['aim']:<6} score={row.get('score')} "
              f"missing={row['missing_aliases'] + row['missing_terms'] or '-'}")

    baseline_ok, baseline_why = usable_as_baseline(report["summary"])
    refusing = bool(a.save_baseline) and not baseline_ok and not a.force_baseline
    if a.out and not (refusing and a.save_baseline and a.out.resolve() == a.save_baseline.resolve()):
        a.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"[eval] wrote {a.out}")
    elif a.out:
        print(f"[eval] not writing {a.out}: it is the baseline path this run is refusing to write",
              file=sys.stderr)
    if a.baseline and a.baseline.exists():
        prior = json.loads(a.baseline.read_text(encoding="utf-8"))
        deltas = scorer.compare(prior.get("rows", []), rows)
        print("\ndeltas vs baseline:" if deltas else "\nno per-question change vs baseline")
        for line in deltas:
            print(line)
    if a.save_baseline:
        ok, why = usable_as_baseline(report["summary"])
        if not ok and not a.force_baseline:
            print(f"\n[eval] REFUSING to save a baseline: {why}.\n"
                  f"       Wait for the engines to recover and re-run, or pass --force-baseline if "
                  f"you deliberately want to freeze a degraded run.", file=sys.stderr)
            return 2
        a.save_baseline.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"[eval] baseline saved to {a.save_baseline}"
              + (" (FORCED over a degraded run)" if not ok else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
