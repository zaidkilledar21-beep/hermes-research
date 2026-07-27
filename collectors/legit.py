"""Legit-source collectors — official APIs / sanctioned readers only. No scraping here.

CLI:  python -m collectors.legit <source> --run <run_id> --query "<q>" [--limit N]
  sources: x | github | youtube | rss | web | hackernews | web_search
Each fetches, normalizes, and stores evidence via common.store_evidence.
Walled sources (reddit/ig/fb) are NOT here — they live in the isolated reach container.
"""
from __future__ import annotations
import argparse
import os
import sys
import requests
from . import common, search

TIMEOUT = 25


def collect_x(run_id: int, query: str, limit: int = 25) -> int:
    """X search (official API, pay-per-use). Reads only; never posts.

    Recent Search covers ONLY THE LAST 7 DAYS — for questions about accumulated operator
    experience that window is structurally near-empty (2 items across the 14-run peptide
    campaign), which is a property of the endpoint, not a query bug. X_SEARCH_ARCHIVE=1 switches
    to /search/all (full archive, back to 2006) — flip it only after the one-off tier probe
    confirms the current pay-as-you-go plan may call it (403 = recent-only tier)."""
    token = os.environ["X_BEARER_TOKEN"]
    archive = os.environ.get("X_SEARCH_ARCHIVE", "0") not in ("0", "false", "no", "")
    endpoint = ("https://api.x.com/2/tweets/search/all" if archive
                else "https://api.x.com/2/tweets/search/recent")
    r = requests.get(
        endpoint,
        headers={"Authorization": f"Bearer {token}"},
        params={"query": query, "max_results": min(limit, 100),
                "tweet.fields": "created_at,public_metrics,author_id"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    n = 0
    for tw in r.json().get("data", []):
        url = f"https://x.com/i/status/{tw['id']}"
        if common.store_evidence(run_id, "x_api", url, tw.get("text", "")):
            n += 1
    return n


def collect_github(run_id: int, query: str, limit: int = 25) -> int:
    """GitHub issues/discussions search via official API (gh token optional, raises limits)."""
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    r = requests.get(
        "https://api.github.com/search/issues",
        headers=headers, params={"q": query, "per_page": min(limit, 100)}, timeout=TIMEOUT,
    )
    r.raise_for_status()
    n = 0
    for it in r.json().get("items", []):
        body = f"{it.get('title','')}\n\n{it.get('body','') or ''}"
        if common.store_evidence(run_id, "github_api", it.get("html_url"), body):
            n += 1
    return n


def collect_youtube(run_id: int, query: str, limit: int = 5) -> int:
    """YouTube transcripts via yt-dlp. `query` is a video URL or watch id here."""
    import subprocess, sys, json, tempfile, glob
    n = 0
    with tempfile.TemporaryDirectory() as td:
        # Invoke as a module, not a bare "yt-dlp" PATH lookup — this process isn't always launched
        # with the venv's bin/ on PATH (e.g. `./.venv/bin/uvicorn` directly, no `activate` sourced),
        # but `sys.executable -m yt_dlp` always resolves the venv's own install correctly.
        subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--skip-download", "--write-auto-subs",
             "--sub-format", "vtt", "--sub-langs", "en", "-o", f"{td}/%(id)s.%(ext)s", query],
            check=False, timeout=120, capture_output=True,
        )
        for vtt in glob.glob(f"{td}/*.vtt"):
            with open(vtt, encoding="utf-8", errors="replace") as f:
                # crude vtt->text: drop timestamps/cue markers
                lines = [ln.strip() for ln in f if "-->" not in ln and ln.strip()
                         and not ln.strip().isdigit() and not ln.startswith("WEBVTT")]
            text = " ".join(dict.fromkeys(lines))  # dedup repeated caption lines, keep order
            if text and common.store_evidence(run_id, "youtube_dl", query, text):
                n += 1
    return n


def collect_hackernews(run_id: int, query: str, limit: int = 25) -> int:
    """Hacker News via the public Algolia API — real practitioner discussion, no login/key.
    Pulls story titles + comment bodies matching the query (community-tier evidence)."""
    n = 0
    for tag, field in (("story", "title"), ("comment", "comment_text")):
        r = requests.get(
            "https://hn.algolia.com/api/v1/search",
            params={"query": query, "tags": tag, "hitsPerPage": min(limit, 50)}, timeout=TIMEOUT,
        )
        r.raise_for_status()
        for hit in r.json().get("hits", []):
            text = hit.get(field) or hit.get("story_text") or ""
            if not text:
                continue
            oid = hit.get("objectID")
            url = f"https://news.ycombinator.com/item?id={oid}" if oid else None
            if common.store_evidence(run_id, "hackernews_api", url, text):
                n += 1
    return n


def collect_rss(run_id: int, url: str, limit: int = 25) -> int:
    """RSS/Atom feed — honors nothing fancy, just reads entries."""
    import feedparser
    feed = feedparser.parse(url)
    n = 0
    for e in feed.entries[:limit]:
        body = f"{e.get('title','')}\n\n{e.get('summary','')}"
        if common.store_evidence(run_id, "rss", e.get("link", url), body):
            n += 1
    return n


def _jina_read(url: str) -> str:
    """Fetch a URL as clean markdown via Jina Reader (r.jina.ai). No login, low risk."""
    headers = {}
    if os.environ.get("JINA_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['JINA_API_KEY']}"
    r = requests.get(f"https://r.jina.ai/{url}", headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def collect_web(run_id: int, url: str, limit: int = 1) -> int:
    """Read one given web page. `url` must be an actual page URL, not a search query."""
    return 1 if common.store_evidence(run_id, "web_reader", url, _jina_read(url)) else 0


# Discovery lives in collectors/search.py (no DB import, so the eval harness can use it without a
# database). Re-exported here so existing callers and the CLI keep working unchanged.
searxng_results = search.searxng_results
discover_urls = search.discover_urls


def read_urls(run_id: int, urls: list[str], source_id: str = "web_search") -> int:
    """Read an ALREADY-SELECTED list of URLs into evidence. Fail-soft per URL.

    Split out from collect_websearch because discovery and reading now happen at different levels:
    the orchestrator pools candidates across several query variants and picks a diverse subset
    (pipeline/select.py) before anything is fetched. Reading is the expensive half, so the decision
    of WHICH urls to read must not be buried inside a per-query collector.
    """
    n = 0
    for u in urls:
        try:
            if common.store_evidence(run_id, source_id, u, _jina_read(u)):
                n += 1
        except Exception as e:  # one dead/blocked page must never sink the batch
            print(f"[collect:{source_id}] read failed {u}: {type(e).__name__}: {e}", file=sys.stderr)
    return n


def collect_websearch(run_id: int, query: str, limit: int = 8) -> int:
    """The open-web search source — the spine the engine was missing. SearXNG finds relevant URLs
    by keyword; the existing Jina reader then pulls each page's full content as evidence (so a
    .gov/.edu hit gets auto-tiered primary_authority via common.web_tier_for).

    Single-query path, kept for the CLI and for any caller that has one query and no plan. The
    pipeline itself goes through the pooled+diversified path in pipeline/run.py instead.
    """
    return read_urls(run_id, discover_urls(query, limit=min(limit, 8)))


# ── v3 Part H: primary-source collectors — free, keyless, grade A / primary_authority. ──────────
# These answer with the documents an expert cites: filings, dockets, enforcement records. Each is
# bounded (limit-capped, content truncated) and fail-soft like every other collector.

def collect_sec_edgar(run_id: int, query: str, limit: int = 10) -> int:
    """SEC EDGAR full-text search (efts.sec.gov, free, keyless). Finds filings mentioning the
    query, then reads each matched document (truncated — extraction caps input anyway).
    SEC requires a descriptive User-Agent with contact; anonymous UAs get 403'd."""
    headers = {"User-Agent": "hermes-research/1.0 (research engine; contact: owner)"}
    r = requests.get("https://efts.sec.gov/LATEST/search-index",
                     params={"q": f'"{query}"'}, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    n = 0
    for hit in (r.json().get("hits", {}).get("hits", []) or [])[:limit]:
        src = hit.get("_source", {})
        accession, _, filename = (hit.get("_id") or "").partition(":")
        cik = (src.get("ciks") or [""])[0].lstrip("0") or "0"
        url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
               f"{accession.replace('-', '')}/{filename}")
        header = (f"SEC filing {src.get('file_type', '?')} — "
                  f"{'; '.join(src.get('display_names', []) or [])} — "
                  f"filed {src.get('file_date', '?')}\n\n")
        body = ""
        try:
            doc = requests.get(url, headers=headers, timeout=TIMEOUT)
            doc.raise_for_status()
            body = doc.text[:20000]
        except Exception:
            body = "(document fetch failed; metadata only)"
        if common.store_evidence(run_id, "sec_edgar", url, header + body):
            n += 1
    return n


def collect_courtlistener(run_id: int, query: str, limit: int = 10) -> int:
    """CourtListener/RECAP opinion search (free tier, keyless at low rate). Litigation involving
    the query — court dockets are where vendor disputes stop being anecdote."""
    r = requests.get("https://www.courtlistener.com/api/rest/v4/search/",
                     params={"q": query, "type": "o", "order_by": "score desc"},
                     timeout=TIMEOUT)
    r.raise_for_status()
    n = 0
    for res in (r.json().get("results", []) or [])[:limit]:
        url = "https://www.courtlistener.com" + (res.get("absolute_url") or "")
        text = (f"{res.get('caseName', '?')} — {res.get('court', '?')} — "
                f"filed {res.get('dateFiled', '?')}\n\n"
                + " ".join(o.get("snippet", "") for o in (res.get("opinions") or [])
                           if isinstance(o, dict))[:8000])
        if common.store_evidence(run_id, "courtlistener", url, text):
            n += 1
    return n


def collect_fda_enforcement(run_id: int, query: str, limit: int = 10) -> int:
    """openFDA enforcement (recall) records across drug + food endpoints (free, keyless).
    NOTE: this is the RECALL database, not warning letters — fda.gov's warning-letter search has
    no stable public API, so letters keep arriving via web_search (where .gov hits auto-tier to
    primary_authority anyway); this collector adds the structured enforcement record lane."""
    n = 0
    per = max(1, limit // 2)
    for endpoint in ("drug", "food"):
        try:
            r = requests.get(f"https://api.fda.gov/{endpoint}/enforcement.json",
                             params={"search": query, "limit": per}, timeout=TIMEOUT)
            if r.status_code == 404:   # openFDA returns 404 for zero matches — not an error
                continue
            r.raise_for_status()
            for res in r.json().get("results", []) or []:
                rid = res.get("recall_number", "?")
                url = f"https://api.fda.gov/{endpoint}/enforcement.json?search=recall_number:{rid}"
                text = (f"FDA {endpoint} enforcement {rid} — {res.get('classification', '?')} — "
                        f"initiated {res.get('recall_initiation_date', '?')} — "
                        f"firm: {res.get('recalling_firm', '?')}\n"
                        f"Product: {res.get('product_description', '')[:1500]}\n"
                        f"Reason: {res.get('reason_for_recall', '')[:1500]}\n"
                        f"Status: {res.get('status', '?')}")
                if common.store_evidence(run_id, "fda_enforcement", url, text):
                    n += 1
        except Exception as e:
            print(f"[collect:fda_enforcement:{endpoint}] {type(e).__name__}: {e}",
                  file=__import__("sys").stderr)
    return n


DISPATCH = {"x": collect_x, "github": collect_github, "youtube": collect_youtube,
            "rss": collect_rss, "web": collect_web, "hackernews": collect_hackernews,
            "web_search": collect_websearch, "sec_edgar": collect_sec_edgar,
            "courtlistener": collect_courtlistener, "fda_enforcement": collect_fda_enforcement}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", choices=DISPATCH.keys())
    ap.add_argument("--run", type=int, required=True)
    ap.add_argument("--query", required=True, help="search query, feed url, page url, or video url")
    ap.add_argument("--limit", type=int, default=25)
    a = ap.parse_args()
    try:
        stored = DISPATCH[a.source](a.run, a.query, a.limit)
    except Exception as e:  # collectors fail soft — log and move on, never crash the run
        print(f"[collect:{a.source}] ERROR {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"[collect:{a.source}] stored {stored} new evidence items for run {a.run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
