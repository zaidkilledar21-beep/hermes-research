"""Shared evidence-store library for all collectors.

Every collector normalizes its source into evidence_items via store_evidence().
Security-critical invariants enforced here so no individual collector can bypass them:
  - content is sanitized (control chars / zero-width / prompt-injection markers stripped to inert)
  - a sha256 content_hash is computed at retrieval and stored (immutability/audit)
  - walled/reach sources are tagged UNTRUSTED_EVIDENCE (downstream injection quarantine)
  - dedup by (run_id, content_hash) — re-collecting the same content is a no-op
"""
from __future__ import annotations
import hashlib
import os
import re
import threading
import time
import unicodedata
import psycopg
from datetime import datetime, timezone

DATABASE_URL = os.environ["DATABASE_URL"]

# Sources whose content must never be trusted as instructions (see plan isolation contract).
# Everything the reach container scrapes is here — forum/review reads are untrusted data too.
WALLED_SOURCES = {"reddit_reach", "reddit_threads", "instagram_reach", "facebook_reach",
                  "stackexchange_reach", "trustpilot_reach", "forum_reach"}

# Web-domain heuristic: a plain web_reader hit on an authority/reference domain is NOT the same
# claim class as a random blog. Bump the tier at ingest so synthesis can weigh it correctly.
# Suffix-matched against the URL host; kept deliberately small and code-only (no LLM call).
_AUTHORITY_SUFFIXES = (".gov", ".gov.uk", ".gov.au", ".gov.pk", ".mil", ".edu",
                       "who.int", "europa.eu", "nih.gov", "fda.gov")
_REFERENCE_HOSTS = ("wikipedia.org", "britannica.com")


def _host(url: str | None) -> str:
    if not url:
        return ""
    from urllib.parse import urlparse
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def web_tier_for(url: str | None) -> str | None:
    """Return an overriding credibility_tier for a generic web read, or None to keep the baseline."""
    host = _host(url)
    if not host:
        return None
    if any(host == s.lstrip(".") or host.endswith(s) for s in _AUTHORITY_SUFFIXES):
        return "primary_authority"
    if any(host == h or host.endswith("." + h) for h in _REFERENCE_HOSTS):
        return "reference"
    return None

# Strip zero-width + bidi control chars commonly used to smuggle hidden prompt-injection text.
_HIDDEN = re.compile(r"[​-‏‪-‮⁠-⁯﻿]")
# Common injection lead-ins get defanged to a visible inert marker (NOT executed, just flagged).
_INJECTION = re.compile(
    r"(?i)\b(ignore (all|previous|above)|disregard (the|all)|system prompt|"
    r"you are now|new instructions?:|exfiltrate|print your (env|secrets?|api key))\b"
)


def sanitize(text: str) -> str:
    """Make retrieved text safe to store and later show to an LLM as DATA, not commands."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _HIDDEN.sub("", text)
    # Neutralize injection lead-ins so a synthesis model can't be steered by scraped content.
    text = _INJECTION.sub(lambda m: f"[quarantined-phrase:{m.group(0)}]", text)
    return text.strip()


def content_hash(raw: str) -> str:
    return "sha256:" + hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


# Provenance fields a community reader may supply per item. Without author/thread identity, ten
# comments in one thread are indistinguishable from ten independent operators — the exact false
# corroboration that makes anecdote dangerous. Corroboration is counted by DISTINCT author/thread.
_META_FIELDS = ("author", "thread_id", "thread_title", "comment_id", "parent_id",
                "item_score", "item_kind", "sort_mode", "depth", "page_ownership")


def store_evidence(run_id: int, source_id: str, url: str | None, raw_content: str,
                   grade: str | None = None, tier: str | None = None,
                   meta: dict | None = None) -> int | None:
    """Insert one evidence row. Returns evidence_id, or None if it was a duplicate.

    grade defaults to the source's registered baseline if not overridden.
    tier (credibility_tier) resolves: explicit arg > web-domain heuristic > source baseline.
    meta carries optional per-item provenance (see _META_FIELDS); unknown keys are ignored.
    """
    clean = sanitize(raw_content)
    h = content_hash(raw_content)  # hash the RAW retrieval, before sanitize, for true audit
    trust = "UNTRUSTED_EVIDENCE" if source_id in WALLED_SOURCES else "TRUSTED_EVIDENCE"
    meta = meta or {}
    extra_cols = [f for f in _META_FIELDS if meta.get(f) is not None]
    extra_vals = [meta[f] for f in extra_cols]
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            baseline_grade, baseline_tier = "C", "general_web"
            cur.execute("SELECT default_grade, credibility_tier FROM sources WHERE source_id=%s",
                        (source_id,))
            row = cur.fetchone()
            if row:
                baseline_grade, baseline_tier = row[0], row[1]
            if grade is None:
                grade = baseline_grade
            if tier is None:
                # A generic web read on a .gov/.edu/reference host outranks its 'general_web' baseline
                # — applies to both the single-page reader and search-found pages.
                web_like = source_id in ("web_reader", "web_search")
                tier = (web_tier_for(url) if web_like else None) or baseline_tier
            cols = ("run_id, source_id, url, retrieved_at, content_hash, content, grade, trust_tag, "
                    "credibility_tier" + ("".join(f", {c}" for c in extra_cols)))
            placeholders = "%s,%s,%s,%s,%s,%s,%s,%s,%s" + ("".join(",%s" for _ in extra_cols))
            cur.execute(
                f"""INSERT INTO evidence_items ({cols})
                   VALUES ({placeholders})
                   ON CONFLICT (run_id, content_hash) DO NOTHING
                   RETURNING evidence_id""",
                (run_id, source_id, url, datetime.now(timezone.utc), h, clean, grade, trust, tier,
                 *extra_vals),
            )
            r = cur.fetchone()
            return r[0] if r else None


def log_agent_run(run_id: int, profile: str, model: str, tokens_in: int, tokens_out: int,
                  cost_usd: float, skill: str | None = None) -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            """INSERT INTO agent_runs
               (run_id, profile, skill, model_actual, tokens_in, tokens_out, cost_usd)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (run_id, profile, skill, model, tokens_in, tokens_out, cost_usd),
        )


# --- Budget accounting -------------------------------------------------------------------------
#
# There used to be ONE function here, `budget_spent(run_id)`, summing `WHERE run_id=%s`. Eight call
# sites compared it against a variable named OPENROUTER_**DAILY**_CAP_USD. So a cap everyone read as
# "the most this engine spends in a day" was in fact enforced per run: 14 concurrent runs meant a $28
# ceiling, and every one of those processes was correctly under its own budget while the day's total
# was 14x what the operator had configured. The bug was invisible for as long as runs were serial,
# because with one run at a time the two numbers are the same number.
#
# The name was the defect. `budget_spent` is deliberately GONE rather than kept as an alias: every
# call site now has to say which budget it means, and cannot be ambiguous by accident again.

RUN_CAP_USD = float(os.environ.get("OPENROUTER_RUN_CAP_USD", "2"))
DAILY_CAP_USD = float(os.environ.get("OPENROUTER_DAILY_CAP_USD", "2"))

# Extraction fans out to MAX_WORKERS threads and consults the budget on every paid call, so an
# uncached cross-run query would mean hundreds of round trips per run. Cost accrues in dollars per
# minute, never per call, so a few seconds of staleness cannot change the verdict at a boundary this
# coarse — but it collapses the query count by two orders of magnitude.
_SPENT_TODAY_TTL = float(os.environ.get("BUDGET_CACHE_TTL_SECONDS", "30"))
_spent_today_cache: tuple[float, float] | None = None   # (monotonic_fetched_at, dollars)
_spent_today_lock = threading.Lock()


def spent_for_run(run_id: int) -> float:
    """Dollars logged against ONE run. Never cached — it is queried at stage boundaries, not per call."""
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM agent_runs WHERE run_id=%s", (run_id,)
        ).fetchone()
        return float(row[0]) if row else 0.0


def spent_today(*, fresh: bool = False) -> float:
    """Dollars logged across EVERY run since local midnight. Cached for _SPENT_TODAY_TTL seconds;
    pass fresh=True at decision points that must not act on a stale number."""
    global _spent_today_cache
    now = time.monotonic()
    if not fresh:
        cached = _spent_today_cache
        if cached is not None and (now - cached[0]) < _SPENT_TODAY_TTL:
            return cached[1]
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM agent_runs WHERE created_at >= current_date"
        ).fetchone()
        value = float(row[0]) if row else 0.0
    with _spent_today_lock:
        _spent_today_cache = (time.monotonic(), value)
    return value


def over_budget(run_id: int, *, fresh: bool = False) -> tuple[bool, str]:
    """True when EITHER this run's own spend or the day's total across all runs has hit its cap.

    Returns (blocked, reason) so callers can log WHICH ceiling stopped them — the per-run and the
    daily limits fail for completely different reasons and the operator needs to tell them apart.
    """
    run_spend = spent_for_run(run_id)
    if run_spend >= RUN_CAP_USD:
        return True, f"run cap ${RUN_CAP_USD:.2f} reached (this run: ${run_spend:.4f})"
    day_spend = spent_today(fresh=fresh)
    if day_spend >= DAILY_CAP_USD:
        return True, f"daily cap ${DAILY_CAP_USD:.2f} reached (today, all runs: ${day_spend:.4f})"
    return False, ""
