"""Deterministic scoring for the discovery-targeting eval — pure functions, no network, no DB.

WHY THIS EXISTS: every retrieval change so far has been argued from a single run's anecdote ("run 28
looked better"). That cannot distinguish a real improvement from a lucky question. The eval set
(evals/questions.json) carries the judgment as hand-written labels, and this module turns a query
plan + a discovery result into numbers, so a change can be shown to help or shown not to.

TWO THINGS THIS SCORER REFUSES TO DO, both of which it did in its first draft:

  1. IT DOES NOT ASK THE GENERATOR WHAT THE RIGHT ANSWER IS. `expect_terms` are literal strings
     hand-written per question, not read from queries.FAILURE_FAMILIES. If someone replaces the
     failure vocabulary with nonsense, the score must FALL; an oracle that imports its answers from
     the code under test always reports success.
  2. IT DOES NOT CREDIT A VENUE FOR EXISTING. A generic /r/logistics thread is exactly what run 28
     retrieved and the relevance filter correctly threw away — so reach is scored on the SUBREDDIT,
     not on reddit.com, and `signal` separately asks whether the URL's own slug carries the vendor
     name or complaint language. Scoring hosts alone gave run 28's failure a perfect 1.0.

WHAT IS MEASURED, and why not precision/recall: there is no ground-truth document set to recall
against — the population of relevant threads is unknowable. What IS knowable is whether the plan
aims at failure language, and whether the read budget lands on practitioner venues carrying complaint
signal instead of vendor SEO surfaces.

  aim         — did the plan anchor the expected failure language ON the vendor the question names
  reach       — did the selected URLs land on venues where the answer plausibly lives
  signal      — do the selected URLs themselves carry complaint/vendor language in their slug
  diversity   — did the read budget span venues, or pile into one
  vendor_pull — how much of the budget went to vendor/affiliate surfaces (penalty)

Scores are proxies and are labelled as such. They compare two versions of the engine on the same
fixed questions; an absolute score means nothing on its own.
"""
from __future__ import annotations

from urllib.parse import urlparse

from pipeline import select

# aim and reach dominate: they are the failure being fixed. signal is the guard against "found a
# reddit thread, therefore good". diversity guards the fix's own failure mode (all reads from one
# lucky venue). vendor_pull is a penalty, so it enters negatively.
WEIGHTS = {"aim": 0.30, "reach": 0.25, "signal": 0.25, "diversity": 0.20, "vendor_pull": -0.20}


def _matches(host: str, listed: list[str]) -> bool:
    """Host-suffix match so 'reddit.com' covers 'old.reddit.com' without listing every subdomain."""
    host = (host or "").lower()
    return any(host == h.lower() or host.endswith("." + h.lower()) for h in listed or [])


def _path(url: str) -> str:
    try:
        return (urlparse(url).path or "").lower()
    except ValueError:
        return ""


def anchored(query: str, term: str, aliases: list[str]) -> bool:
    """True when ONE query carries both the term and (if the question names a vendor) an alias.

    Checking the alias and the term independently across the whole plan was worth nothing: an alias
    in query 1 and a bare 'reserve' in query 4 would score full marks while retrieving every
    payments complaint on the internet. Anchoring is the entire mechanism being tested.
    """
    q = query.lower()
    if term.lower().strip('"') not in q:
        return False
    return not aliases or any(a.lower() in q for a in aliases)


def plan_metrics(spec: dict, plan: list[str]) -> dict:
    """Score the QUERY PLAN alone — runs fully offline, which is what makes this eval cheap enough
    to run on every change. `plan` is the flattened list of queries the engine would issue."""
    aliases = spec.get("expect_aliases", [])
    terms = spec.get("expect_terms", [])
    alias_hits = [a for a in aliases if any(a.lower() in q.lower() for q in plan)]
    term_hits = [t for t in terms if any(anchored(q, t, aliases) for q in plan)]
    # A control question with nothing to expect scores 1.0 for aim: it exists to prove no
    # REGRESSION elsewhere (its reach/vendor_pull carry the real verdict), and scoring it 0 would
    # punish the engine for correctly having nothing to aim at.
    alias_score = len(alias_hits) / len(aliases) if aliases else 1.0
    term_score = len(term_hits) / len(terms) if terms else 1.0
    return {
        "queries": len(plan),
        "alias_coverage": round(alias_score, 3),
        "term_coverage": round(term_score, 3),
        "aim": round((alias_score + term_score) / 2, 3),
        "missing_aliases": [a for a in aliases if a not in alias_hits],
        "missing_terms": [t for t in terms if t not in term_hits],
    }


def venue_of(url: str) -> str:
    """The venue a URL belongs to: the subreddit for Reddit, the registrable domain otherwise."""
    return select.reddit_key(url)


def is_relevant_venue(spec: dict, url: str) -> bool:
    """Reddit is judged on its SUBREDDIT. Crediting reddit.com itself is how generic chatter scored
    a perfect run — the host is not the venue, the community is."""
    venue = venue_of(url)
    if venue.startswith("r/"):
        listed = [s.lower().lstrip("/") for s in spec.get("relevant_subreddits", [])]
        return venue.lower() in listed or venue.lower().lstrip("r/") in listed
    return _matches(select.domain_key(url), spec.get("relevant_hosts", []))


def has_signal(spec: dict, url: str) -> bool:
    """Does the URL's own slug carry hand-written COMPLAINT language?

    Reddit thread URLs embed the post title, so this reads real page signal without fetching
    anything. `signal_terms` are written per question by hand — never derived from the generator,
    and deliberately never including the vendor's name: `/3plguys_launches_new_service/` mentions
    the vendor and is not a complaint, so crediting aliases here would have made the metric agree
    with any on-topic page at all.
    """
    path = _path(url)
    if not path:
        return False
    return any(t.lower() in path for t in spec.get("signal_terms", []) if t)


def discovery_metrics(spec: dict, selected: list[str]) -> dict:
    """Score what the run would actually READ — the set that costs money and fills the evidence
    budget."""
    if not selected:
        return {"selected": 0, "reach": 0.0, "signal": 0.0, "diversity": 0.0, "vendor_pull": 0.0,
                "venues": 0, "thread_share": 0.0}
    venues = {venue_of(u) for u in selected}
    reach = sum(1 for u in selected if is_relevant_venue(spec, u)) / len(selected)
    signal = sum(1 for u in selected if has_signal(spec, u)) / len(selected)
    pull = sum(1 for u in selected
               if _matches(select.domain_key(u), spec.get("penalty_hosts", []))) / len(selected)
    threads = sum(1 for u in selected if "/comments/" in _path(u))
    return {
        "selected": len(selected),
        "venues": len(venues),
        "reach": round(reach, 3),
        "signal": round(signal, 3),
        "diversity": round(len(venues) / len(selected), 3),
        "vendor_pull": round(pull, 3),
        "thread_share": round(threads / len(selected), 3),
    }


def composite(metrics: dict) -> float:
    """One comparable number per question. Clamped to [0, 1] so a heavy vendor_pull penalty cannot
    make a run look worse than having found nothing at all."""
    total = sum(WEIGHTS[k] * float(metrics.get(k) or 0.0) for k in WEIGHTS)
    positive = sum(w for w in WEIGHTS.values() if w > 0)
    return round(max(0.0, min(1.0, total / positive)), 3)


# Generic connective/query tokens that would inflate novelty without carrying domain signal.
# Small on purpose: the metric compares two planners on the SAME questions, so a systematic
# background rate cancels out; only obviously-empty tokens are excluded.
_VOCAB_STOPWORDS = {"the", "a", "an", "and", "or", "not", "for", "with", "about", "what", "how",
                    "who", "which", "does", "do", "are", "is", "review", "reviews", "experience",
                    "anyone", "tried", "problems", "site", "com"}


def vocabulary_metrics(spec: dict, plan: list[str]) -> dict:
    """v3, REPORTED NOT SCORED: fraction of the plan's content tokens that do not appear in the
    question — a domain-agnostic proxy for "did the planner add vocabulary the deterministic path
    could never add" (that path can only delete words). Deliberately outside WEIGHTS/composite():
    over-eager expansion could hurt signal/vendor_pull as easily as help reach, so novelty earns a
    weight only if bakeoffs show it tracks reach up, not vendor_pull up."""
    import re as _re
    q_tokens = {w.lower() for w in _re.findall(r"[a-zA-Z]{3,}", spec.get("question", ""))}
    plan_tokens = ({w.lower() for w in _re.findall(r"[a-zA-Z]{3,}", " ".join(plan))}
                   - _VOCAB_STOPWORDS)
    novel = sorted(plan_tokens - q_tokens)
    return {"novel_tokens": novel,
            "novelty_rate": round(len(novel) / len(plan_tokens), 3) if plan_tokens else 0.0}


def score_question(spec: dict, plan: list[str], selected: list[str] | None) -> dict:
    """Full per-question result. `selected=None` means plan-only (offline) mode: the discovery
    components are reported as None rather than 0, so an offline run is never mistaken for a run
    where discovery found nothing."""
    out = {"id": spec.get("id"), **plan_metrics(spec, plan)}
    if selected is None:
        out.update({"selected": None, "reach": None, "signal": None, "diversity": None,
                    "vendor_pull": None, "score": None, "mode": "plan-only"})
        return out
    out.update(discovery_metrics(spec, selected))
    out["mode"] = "discovery"
    out["score"] = composite(out)
    return out


def compare(baseline: list[dict], current: list[dict]) -> list[str]:
    """Human-readable deltas between two eval reports, worst regression first.

    Falls back to `aim` only when BOTH sides are plan-only — a plan-only baseline stores score=None,
    and silently skipping those rows made the harness report "no change" while aim had moved from
    0.65 to 1.0. Falling back when only ONE side lacks a score would be worse than either: a
    discovery baseline compared against a plan-only fallback would print "UP [aim]" for a run where
    discovery was never evaluated at all. Mixed modes are skipped and named.
    """
    by_id = {r.get("id"): r for r in baseline}
    lines: list[str] = []
    for row in current:
        prior = by_id.get(row.get("id"))
        if not prior:
            lines.append(f"  NEW      {row.get('id')}: score={row.get('score')}")
            continue
        has_scores = (prior.get("score") is not None, row.get("score") is not None)
        if has_scores[0] != has_scores[1]:
            lines.append(f"  SKIP     {row.get('id')}: baseline and current ran in different modes")
            continue
        metric = "score" if all(has_scores) else "aim"
        old, new = prior.get(metric), row.get(metric)
        if old is None or new is None:
            continue
        delta = round(new - old, 3)
        if abs(delta) >= 0.001:
            lines.append(f"  {'UP  ' if delta > 0 else 'DOWN'}     {row.get('id')} [{metric}]: "
                         f"{old} -> {new} ({delta:+})")
    lines.sort(key=lambda line: line.strip().startswith("UP"))
    return lines
