"""SearXNG discovery — find candidate URLs. No reading, no storage, no database.

Split out of collectors/legit.py deliberately: legit imports the evidence store, which requires
psycopg and a live DATABASE_URL at import time. Discovery itself needs neither, and the eval harness
(evals/run_eval.py) must be runnable on a laptop with nothing but this repo and a reachable SearXNG.
Keeping the two apart is also the honest boundary — discovery decides what MIGHT be worth reading,
and nothing it returns has touched the evidence chain yet.
"""
from __future__ import annotations

import os
import sys
import threading
import time

import requests

from pipeline.select import float_env, int_env

TIMEOUT = int_env("SEARXNG_TIMEOUT", 25)
# Minimum spacing between discovery queries. Failure-language families took queries per sub-question
# from 1-2 to ~5, and SearXNG's upstream engines answer that burst with "unusual traffic from your
# network" (Google CSE) and 403s (DuckDuckGo), each suspending the engine for 180s. Pacing costs
# seconds; a suspended engine costs a whole run's discovery.
PACING_SECONDS = float_env("SEARXNG_PACING", 1.5)
# One retry after an upstream suspension. 20s rather than 8s: the observed suspensions are
# `suspended_time=180`, so a short backoff mostly retries into the same wall. This does not wait the
# suspension out (that would stall a run for minutes) — it just stops the retry being pure noise.
THROTTLE_BACKOFF = float_env("SEARXNG_BACKOFF", 20.0)
# Retries are extra upstream requests that the caller's per-run search budget never authorised, so
# they get their own process-wide ceiling.
MAX_RETRIES = int_env("SEARXNG_MAX_RETRIES", 10)
# Cross-process pacing state. Research runs execute as SEPARATE PROCESSES (two in flight during a
# campaign), so an in-process lock spaces one run's queries and does nothing about the aggregate
# rate the proxy and the engines actually see — which is the rate that gets us suspended.
PACE_FILE = os.environ.get("SEARXNG_PACE_FILE", "/tmp/hermes-searxng.pace")

_pace_lock = threading.Lock()
# Queries that came back EMPTY because upstream engines were suspended. Read by the orchestrator so
# a throttled run is never reported as a run that found nothing.
throttled_queries = 0
# Queries that RETURNED RESULTS while some engines were suspended. Not a failure, but not a clean
# measurement either: the result set is missing whatever those engines would have contributed, and
# the eval must not freeze a baseline from a partially-degraded window.
degraded_queries = 0
_retries_used = 0


def base_url() -> str:
    return os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888").rstrip("/")


def _pace() -> None:
    """Space out queries across EVERY process on this box, not just this one.

    Concurrent research runs are separate processes sharing one SearXNG and one proxy exit, so the
    rate that matters is the aggregate. The pace mark is a file's mtime guarded by an exclusive
    flock: whoever holds the lock sleeps out the remainder of the interval and stamps the file, so
    two runs interleave at PACING_SECONDS apart instead of firing in lockstep pairs.

    Fail-open: if the lock file cannot be used (permissions, a platform without flock), pacing
    degrades to in-process only rather than blocking discovery.
    """
    if PACING_SECONDS <= 0:
        return
    with _pace_lock:
        try:
            import fcntl

            fd = os.open(PACE_FILE, os.O_RDWR | os.O_CREAT, 0o666)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                last = os.fstat(fd).st_mtime
                wait = PACING_SECONDS - (time.time() - last)
                if wait > 0:
                    time.sleep(min(wait, PACING_SECONDS))
                os.utime(fd, None)
            finally:
                os.close(fd)
        except Exception:
            time.sleep(PACING_SECONDS)


def searxng_query(query: str, pageno: int = 1) -> tuple[list[dict], list]:
    """Return (results, unresponsive_engines) for one query. One place knows the endpoint shape.

    `unresponsive_engines` is what makes a throttled search distinguishable from a search that
    genuinely found nothing — SearXNG answers 200 with an empty result list in BOTH cases, and
    treating them alike is how a rate limit turns into a confident "no evidence exists".
    """
    _pace()
    params = {"q": query, "format": "json"}
    if pageno > 1:
        params["pageno"] = pageno
    r = requests.get(f"{base_url()}/search", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    body = r.json()
    return body.get("results", []), body.get("unresponsive_engines", []) or []


def searxng_results(query: str, pageno: int = 1) -> list[dict]:
    """Results only — kept for callers that do not care why a search came back empty."""
    return searxng_query(query, pageno)[0]


def discover_urls(query: str, site: str | None = None, limit: int = 10,
                  path_must_contain: str | None = None) -> list[str]:
    """Find candidate URLs for a topic — DISCOVERY only, no reading, no storage.

    Exists because a platform's own search is often far worse than a general search engine's index
    of that platform. Reddit search returned anime/AITAH posts for niche B2B queries, while
    `site:reddit.com <query>` through SearXNG returned on-target threads in dedicated subreddits.
    `path_must_contain` filters to a URL shape (e.g. '/comments/' = an actual thread, not a listing).
    """
    global throttled_queries, degraded_queries, _retries_used
    q = f"site:{site} {query}" if site else query
    seen, urls = set(), []
    try:
        results, unresponsive = searxng_query(q)
        if unresponsive and results:
            # Partial degradation: some engines answered, some are suspended. Counted separately —
            # it is not a failed query, but the result set is missing whatever the suspended engines
            # would have contributed, and a baseline frozen from this window would be wrong.
            degraded_queries += 1
            print(f"[discover] partial results for '{q}' — engines unresponsive: "
                  f"{_names(unresponsive)}", file=sys.stderr)
        elif not results and unresponsive:
            # Empty + suspended engines = throttled, not absent. Count it BEFORE the retry: if the
            # retry itself raises, the throttling still happened and the run must still know.
            throttled_queries += 1
            if _retries_used < MAX_RETRIES:
                _retries_used += 1
                print(f"[discover] upstream engines unresponsive ({_names(unresponsive)}) — "
                      f"retrying '{q}' in {THROTTLE_BACKOFF}s", file=sys.stderr)
                time.sleep(THROTTLE_BACKOFF)
                results, unresponsive = searxng_query(q)
                if results:
                    throttled_queries -= 1   # the retry recovered it; not a lost query after all
                    if unresponsive:
                        degraded_queries += 1
                else:
                    print(f"[discover] '{q}' still empty after backoff; engines: "
                          f"{_names(unresponsive)}. THROTTLED search, not an empty topic.",
                          file=sys.stderr)
            else:
                print(f"[discover] '{q}' empty, engines unresponsive ({_names(unresponsive)}); "
                      f"retry budget spent", file=sys.stderr)
    except Exception as e:
        print(f"[discover] search failed: {type(e).__name__}: {e}", file=sys.stderr)
        return []
    for res in results:
        u = res.get("url")
        if not u or u in seen:
            continue
        if path_must_contain and path_must_contain not in u:
            continue
        seen.add(u)
        urls.append(u)
        if len(urls) >= limit:
            break
    return urls


def _names(unresponsive) -> str:
    """SearXNG reports unresponsive engines as [[name, reason], ...] (shape varies by version)."""
    out = []
    for entry in unresponsive or []:
        out.append(str(entry[0]) if isinstance(entry, (list, tuple)) and entry else str(entry))
    return ", ".join(out) or "unknown"


def reachable(timeout: float = 3.0) -> bool:
    """Is the CONFIGURED endpoint actually a working SearXNG JSON API?

    Deliberately a real query, not a TCP connect: a wrong reverse-proxy path, an unrelated service
    on the port, or a SearXNG with the JSON format disabled all answer a TCP handshake happily, and
    the caller would then run every query into an empty result set and report a confident zero
    instead of degrading to offline mode.
    """
    try:
        r = requests.get(f"{base_url()}/search", params={"q": "ping", "format": "json"},
                         timeout=timeout)
        r.raise_for_status()
        return isinstance(r.json().get("results"), list)
    except Exception:
        return False
