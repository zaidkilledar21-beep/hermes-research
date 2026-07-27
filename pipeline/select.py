"""Diversity-aware candidate selection — retrieve wide, read diverse.

WHY: retrieval volume stopped being the bottleneck at run 28 (166 evidence items from 121 distinct
authors); CONCENTRATION became one. A search engine happily returns eight URLs from the same host,
and a single subreddit can supply every community thread in a run — which downstream reads as broad
corroboration when it is really one venue's opinion, restated. Reading is also the expensive half
(a Jina fetch per web page, a full browser render per Reddit thread on a 4GB box), so the read
budget has to be spent on DIFFERENT venues, not on the loudest one.

The fix is deliberately dumb and deterministic — no model call. Discovery pools candidates across
every query variant, then this module round-robins across venues: one URL from each venue before a
second from any, capped per venue and in total. Same input always yields the same selection, which
is what makes the eval harness able to prove a change helped.

Two structures exist because pooling naively would have silently wasted the sharper aim:

  interleave()   — candidates are pooled from several queries. Concatenating them puts every hit of
                   the FIRST query ahead of every hit of the others, so with a tight read cap the
                   failure-language queries could pay for four extra searches and change nothing.
                   Interleaving takes each query's best hit first.
  select_tiered()— the base topic query and the failure-language queries are different KINDS of
                   candidate. A regulatory question ("FDA labeling enforcement") must not have its
                   primary sources displaced by "RUO reserve" hits, so the base tier holds a
                   reserved floor of the read budget that the failure tier cannot take.

`priority` (from the vertical source registry — pipeline/registry.py) reorders venues, in the
registry's OWN ranked order. That is the ONLY thing the registry does here: it never adds a venue
discovery did not find, so a stale entry can bias read order but can never manufacture evidence.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Sequence
from urllib.parse import urlparse, urlsplit, urlunsplit


def int_env(name: str, default: int) -> int:
    """Env knob that cannot take the process down. A malformed value in a .env used to raise at
    IMPORT time — outside every try/except in the pipeline — turning a typo into a dead run."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        print(f"[select] ignoring malformed {name}={os.environ.get(name)!r}, using {default}")
        return default


def float_env(name: str, default: float, lo: float = 0.0, hi: float | None = None) -> float:
    """Same contract as int_env for fractional knobs. NaN/inf are rejected too — they parse fine and
    then detonate later inside an int() conversion, which is a worse failure than a startup log.

    `hi` defaults to unbounded: this helper is used for both fractions (BASE_TIER_FLOOR, 0-1) and
    durations (search pacing, seconds). Hard-coding the 0-1 range made every duration override look
    malformed and silently snap back to its default — the values happened to match, so it would have
    gone unnoticed until someone tried to actually change one.
    """
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = float("nan")
    if (value != value or value in (float("inf"), float("-inf"))
            or value < lo or (hi is not None and value > hi)):
        print(f"[select] ignoring malformed {name}={os.environ.get(name)!r}, using {default}")
        return default
    return value


# --- Discovery + read budget -------------------------------------------------------------------
# These live HERE, not in the orchestrator, because they are selection policy and because the eval
# harness has to score the exact budget production uses. Two copies of "how many pages do we read"
# would drift, and a drifted eval proves nothing.
#
# Candidates per individual query — cheap, one SearXNG call, nothing fetched.
DISCOVER_PER_QUERY = int_env("DISCOVER_PER_QUERY", 10)
# Open-web pages actually read per sub-question (one Jina fetch each).
WEB_READS = int_env("WEB_SEARCH_READS", 12)
# Community threads read per sub-question (one full browser render each, on a 4GB box).
REDDIT_THREADS = int_env("REDDIT_THREADS", 8)
# Per-venue ceilings inside those budgets.
WEB_PER_DOMAIN = int_env("WEB_SEARCH_PER_DOMAIN", 2)
REDDIT_PER_SUB = int_env("REDDIT_PER_SUBREDDIT", 3)
# Share of the read budget reserved for the base topic query, so failure-language expansion can
# never take the whole budget on a question that was not about failure at all.
BASE_TIER_FLOOR = float_env("BASE_TIER_FLOOR", 0.5, lo=0.0, hi=1.0)
# Slots reserved for registry-scoped discovery (venues proven on past runs). Small and absolute on
# purpose: enough that a known-good venue is always read, never enough for the registry to decide
# most of a run. Past runs get a seat, not the wheel.
REGISTRY_TIER_FLOOR = int_env("REGISTRY_TIER_FLOOR", 2)

_REDDIT_SUB = re.compile(r"/r/([A-Za-z0-9_]+)", re.I)
# Second-level labels that are part of the SUFFIX, not the name: vendor.co.uk and vendor.com.au are
# one publisher, but so are a.co.in and b.co.in — DIFFERENT ones. A hard-coded list of full suffixes
# silently collapsed every unlisted three-label host (a.co.in and b.co.in both became "co.in",
# capping four independent publishers at one venue's allowance). Matching on the generic SLD label
# instead covers the whole family without shipping a public-suffix database.
_GENERIC_SLDS = {"co", "com", "net", "org", "gov", "edu", "ac", "mil", "int", "or", "ne", "go"}
# Query parameters that identify a campaign, not a document. Stripped before dedup so the same page
# arriving from two searches is not read (and billed, and rendered) twice.
_TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "msclkid", "ref_", "share_id", "rdt", "correlation_id")


def domain_key(url: str) -> str:
    """Registrable-ish domain for a URL, lowercased. Empty string if unparseable.

    Collapses subdomains on purpose: support.vendor.com, blog.vendor.com and docs.vendor.com are one
    publisher with one point of view, and giving each its own per-domain allowance is how a vendor
    quietly takes the whole read budget while the diversity metric still looks healthy.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if len(parts) >= 3 and parts[-2] in _GENERIC_SLDS and len(parts[-1]) <= 3:
        return ".".join(parts[-3:])          # vendor.co.uk, vendor.co.in, vendor.com.au
    return ".".join(parts[-2:])              # blog.vendor.com -> vendor.com


def is_reddit(host: str) -> bool:
    """Exact host match, not a suffix test: `notreddit.com` ends with "reddit.com" and would
    otherwise canonicalize into Reddit's namespace and displace a real thread during dedup."""
    host = (host or "").lower()
    return host == "reddit.com" or host.endswith(".reddit.com")


# v3 Part K: forum-thread URL shapes. Niche vertical boards are where operators actually live
# (the peptide boards, not just Reddit), and many of them block plain readers while rendering
# fine in a real browser — which is exactly what the reach container's forum_reach handler is
# for. Path fragments cover the big engines: XenForo (/threads/), phpBB (viewtopic), vBulletin
# (showthread), Discourse (/t/<slug>/<id>), generic (/forum(s)/, /topic/, /boards/).
_FORUM_PATH = re.compile(
    r"/(threads?|forums?|topic|boards?|community|discussion)/|viewtopic|showthread"
    r"|/t/[^/]+/\d+", re.I)


def forum_shaped(url: str) -> bool:
    """True when a discovered URL looks like a forum thread on a NON-reddit host. Reddit has its
    own dedicated path (reddit_threads); this exists so other boards reach the browser reader
    instead of a plain fetch that their anti-bot layer rejects."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if is_reddit(domain_key(url)):
        return False
    return bool(_FORUM_PATH.search(parsed.path or ""))


def reddit_key(url: str) -> str:
    """`r/<subreddit>` for a Reddit URL, else the domain. The venue that matters for Reddit is the
    SUBREDDIT, not the host — every thread shares the host, so keying on domain would collapse all
    candidates into one group and defeat the round-robin entirely."""
    host = domain_key(url)
    if host == "reddit.com":
        try:
            match = _REDDIT_SUB.search(urlparse(url).path or "")
        except ValueError:
            return host
        if match:
            return f"r/{match.group(1).lower()}"
    return host


def canonical(url: str) -> str:
    """Comparison form of a URL. Never returned to a caller — dedupe() hands back the ORIGINAL.

    old./www./new.reddit.com serve the same thread, and a tracking parameter makes two identical
    pages look distinct. Both used to cost a duplicate Jina fetch, or worse, a duplicate 30-60s
    browser render of a thread already read.
    """
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url.split("#", 1)[0])
    except ValueError:
        return url.lower()
    host = (parts.hostname or "").lower()
    if is_reddit(host):
        host = "reddit.com"
    elif host.startswith("www."):
        host = host[4:]
    query = "&".join(sorted(
        kv for kv in (parts.query or "").split("&")
        if kv and not any(kv.lower().startswith(p) for p in _TRACKING_PARAMS)))
    path = (parts.path or "").rstrip("/") or "/"
    return urlunsplit(("https", host, path, query, ""))


def dedupe(urls: Iterable[str]) -> list[str]:
    """First-seen-wins dedup across pooled query variants, on the canonical form. Order is preserved
    because it carries the search engines' own ranking, the only relevance signal available before
    anything is read."""
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if not url or not isinstance(url, str):
            continue
        key = canonical(url)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(url)
    return out


def interleave(pools: Sequence[Sequence[str]]) -> list[str]:
    """Round-robin several candidate lists into one, best-of-each-first.

    Concatenation would rank every hit of the first query above every hit of the others; with a read
    cap of 8 that means the four failure-language queries can cost four searches and contribute
    nothing. Interleaving makes each query's top hit compete on equal footing.
    """
    lists = [list(p) for p in pools if p]
    out: list[str] = []
    for rank in range(max((len(p) for p in lists), default=0)):
        for pool in lists:
            if rank < len(pool):
                out.append(pool[rank])
    return out


def _venues(candidates: Sequence[str], key: Callable[[str], str],
            priority: Sequence[str] | None) -> tuple[dict[str, list[str]], list[str]]:
    """Group candidates by venue and order the venues: registry-preferred first (in the REGISTRY'S
    ranked order, not alphabetically and not by search rank), then by search rank."""
    ranked = {p.lower(): i for i, p in enumerate(priority or []) if p}
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for url in dedupe(candidates):
        venue = key(url) or ""
        if venue not in groups:
            groups[venue] = []
            order.append(venue)
        groups[venue].append(url)
    seen_at = {venue: i for i, venue in enumerate(order)}
    order.sort(key=lambda v: (ranked.get(v.lower(), len(ranked)), seen_at[v]))
    return groups, order


def _take(groups: dict[str, list[str]], order: list[str], *, budget: int, per_key_cap: int,
          used: dict[str, int], taken: set[str], out: list[str]) -> None:
    """Round-robin `budget` more URLs into `out`, respecting the GLOBAL per-venue counts in `used`.

    `used`/`taken` are shared across tiers, so a venue cannot get its full allowance twice by
    appearing in both the base and the failure tier.
    """
    for rank in range(per_key_cap):
        progressed = False
        for venue in order:
            if budget <= 0:
                return
            if used.get(venue, 0) >= per_key_cap:
                continue
            bucket = [u for u in groups[venue] if canonical(u) not in taken]
            if not bucket:
                continue
            url = bucket[0]
            out.append(url)
            taken.add(canonical(url))
            used[venue] = used.get(venue, 0) + 1
            budget -= 1
            progressed = True
        if not progressed:
            return


def select_tiered(tiers: Sequence[Sequence[str]], *, total_cap: int, per_key_cap: int,
                  key: Callable[[str], str] = domain_key,
                  priority: Sequence[str] | None = None,
                  floors: Sequence[int] | None = None) -> list[str]:
    """Select across candidate tiers, giving each tier a reserved floor before the rest competes.

    tiers[0] is the base topic query's candidates; tiers[1:] are expansions. floors[i] is the
    minimum number of slots tier i gets first. After the floors are satisfied every remaining slot
    is filled from all tiers together, still round-robin across venues and still globally capped
    per venue. Pure function — no network, no DB.
    """
    if total_cap <= 0 or per_key_cap <= 0:
        return []
    tiers = [list(t) for t in tiers]
    floors = list(floors or [])
    out: list[str] = []
    used: dict[str, int] = {}
    taken: set[str] = set()

    for i, tier in enumerate(tiers):
        floor = min(floors[i] if i < len(floors) else 0, total_cap - len(out))
        if floor <= 0 or not tier:
            continue
        groups, order = _venues(tier, key, priority)
        _take(groups, order, budget=floor, per_key_cap=per_key_cap,
              used=used, taken=taken, out=out)

    remaining = total_cap - len(out)
    if remaining > 0:
        groups, order = _venues(interleave(tiers), key, priority)
        _take(groups, order, budget=remaining, per_key_cap=per_key_cap,
              used=used, taken=taken, out=out)
    return out[:total_cap]


def select_diverse(candidates: Sequence[str], *, total_cap: int, per_key_cap: int,
                   key: Callable[[str], str] = domain_key,
                   priority: Iterable[str] | None = None) -> list[str]:
    """Single-pool selection: at most `total_cap` URLs, at most `per_key_cap` per venue, spread
    across venues. Thin wrapper over select_tiered for callers with one undifferentiated pool."""
    return select_tiered([candidates], total_cap=total_cap, per_key_cap=per_key_cap,
                         key=key, priority=list(priority or []))
