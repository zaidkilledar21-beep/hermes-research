"""Vertical source registry — cross-run memory of WHICH venues produce evidence that answers.

Every run before this module started discovery from zero, so the engine kept rediscovering (and
re-rejecting) the same generic venues. Run 28 retrieved 166 items and the relevance filter correctly
threw away 156 of them: generic r/logistics chatter, not peptide operators. Nothing recorded that
r/logistics had already failed this topic, or that some niche subreddit had already worked.

WHAT IS RECORDED: one row per (venue, topic), where a venue is a subreddit or a host, and usefulness
is the EXTRACTOR'S OWN verdict (`evidence_items.answers_question`) — not item count, because item
count was already high and still useless. Topics are matched by TOKEN OVERLAP rather than exact
string, since no two questions are phrased the same way.

FOUR DEFENCES AGAINST THE OBVIOUS FAILURE MODE — a registry that reinforces its own past choices:

  1. Promotion is by DISTINCT RUN, not by item. `useful_runs >= MIN_USEFUL_RUNS` (2 by default), so
     two replies in one thread of one run cannot promote a venue. Same principle as counting
     corroboration by distinct author instead of by comment.
  2. Promotion is REVOCABLE. Priority buys a venue extra exposure, and that exposure is what would
     otherwise generate the evidence that justifies the exposure. Because exposure also raises
     total_items, a venue that stops producing answering evidence falls below MIN_USEFUL_RATE and
     drops out of preferred() by itself. Nothing here is a one-way ratchet.
  3. Writes are idempotent, ledgered per run (`vertical_source_runs`). Re-running a stage cannot
     inflate a venue's score; a retry is not evidence.
  4. Vendor-marketing tiers AND vendor-owned/affiliate/sponsored pages earn NO credit (see
     _CREDIT_TIERS / _BLOCKED_OWNERSHIP). A vendor-run subreddit carries tier='community', so the
     tier filter alone cannot see it — ownership has to be checked separately, or the vendor's own
     venue promotes itself into permanent priority.

WHAT IT IS ALLOWED TO DO: reorder venues that live discovery already returned (via select's
`priority`) and add a small, capped number of site-scoped discovery queries. It can never inject
evidence, never bypass extraction, never touch the citation path. A wrong row costs read-budget
ordering and nothing else — deliberately, because this is the engine's first piece of persistent
state and its failure mode has to stay cheap.

KNOWN LIMITATION: the topic is the RUN's question, not the sub-question, because evidence rows carry
no sub-question id. A venue learned for one facet of a run is therefore visible to the run's other
facets. Acceptable while topics are narrow; revisit if sub-question provenance is ever added.

Everything DB-touching is fail-soft with an explicit statement timeout, and imports psycopg lazily,
so the pure helpers stay unit-testable with no database at all.
"""
from __future__ import annotations

import os
import re
import sys
from urllib.parse import urlparse

from pipeline import queries, select
from pipeline.select import float_env, int_env

# A venue must have produced answering evidence in this many DISTINCT RUNS before it influences
# targeting. 1 would let a single lucky run promote a venue permanently.
MIN_USEFUL_RUNS = int_env("REGISTRY_MIN_USEFUL_RUNS", 2)
# ...and must KEEP earning it. Promotion is revocable: registry priority buys a venue more exposure,
# which raises total_items; if the extra exposure stops producing answering evidence the hit rate
# falls below this floor and the venue drops out of preferred() on its own. Without a revocation
# path, one lucky promotion feeds itself forever — the exposure it wins is what generates the
# evidence that justifies the exposure.
MIN_USEFUL_RATE = float_env("REGISTRY_MIN_RATE", 0.15)
# How many topic tokens define a topic. Too few over-matches (every logistics question looks the
# same); too many under-matches (nothing ever overlaps).
TOPIC_TOKENS = int_env("REGISTRY_TOPIC_TOKENS", 5)
# Tokens a past topic must share with the current one to count as the same subject. 1 is far too
# loose — "peptides" alone would drag a payments venue into a shipping question.
MIN_TOPIC_OVERLAP = int_env("REGISTRY_MIN_OVERLAP", 2)
# Registry-derived, site-scoped discovery queries per sub-question. Bounded hard: these are extra
# search calls, and an over-trusted registry would tunnel the run into venues it already knows.
MAX_REGISTRY_QUERIES = int_env("REGISTRY_MAX_QUERIES", 2)
# Rows examined per lookup. The table is small, but "small" is not a guarantee — bound the read.
LOOKUP_ROW_LIMIT = int_env("REGISTRY_ROW_LIMIT", 500)
# Age-out. The rate floor only revokes a venue WITHIN the topic row that keeps being written; a
# neighbouring question writes a DIFFERENT (kind, identifier, topic_key) row, so an old high-rate
# row would otherwise stay eligible forever on the strength of evidence nobody has re-confirmed.
# A venue has to keep showing up in recent runs to keep steering discovery.
MAX_AGE_DAYS = int_env("REGISTRY_MAX_AGE_DAYS", 120)
# Milliseconds. A registry query must never be the thing that hangs a run.
STATEMENT_TIMEOUT_MS = int_env("REGISTRY_TIMEOUT_MS", 10000)

# Credibility tiers whose items may earn a venue credit. A vendor's own marketing always ranks and
# always looks relevant; letting it earn priority would build a registry of advertisers.
_CREDIT_TIERS = {"community", "independent_review", "primary_authority", "reference"}
# Page ownership that disqualifies an item from earning credit REGARDLESS of tier. A vendor-run
# subreddit or a vendor's guest posts carry credibility_tier='community' — the tier filter alone
# cannot see them, and a vendor-controlled venue promoting itself is precisely what the tier filter
# was added to stop.
_BLOCKED_OWNERSHIP = {"vendor_owned", "affiliate_leadgen", "sponsored"}

_REDDIT_SUB = re.compile(r"/r/([A-Za-z0-9_]+)", re.I)
# Venues that are never worth remembering as a "vertical": link shorteners, generic aggregators and
# platform roots that carry no topical identity of their own.
_IGNORED_HOSTS = {"", "reddit.com", "google.com", "bing.com", "duckduckgo.com", "t.co", "bit.ly",
                  "youtu.be", "webcache.googleusercontent.com", "archive.org", "web.archive.org"}


def topic_tokens(question: str, limit: int = TOPIC_TOKENS) -> list[str]:
    """Normalized topic fingerprint for a question — deterministic, entity-biased.

    Reuses compress_for_search so the fingerprint is built from the SAME tokens discovery searches
    on. Truncation happens in COMPRESSED ORDER (entities first), not alphabetically: sorting before
    truncating dropped the vendor name whenever it sorted late, which is how two questions about
    different vendors ended up with the same topic. The survivors are then sorted so the key is
    stable regardless of how the question was worded.
    """
    compressed = queries.compress_for_search(question or "")
    ordered: list[str] = []
    for word in compressed.split():
        token = word.strip(",.;:\"'").lower()
        if len(token) > 1 and token not in ordered:
            ordered.append(token)
        if len(ordered) >= limit:
            break
    return sorted(ordered)


def topic_key(question: str, limit: int = TOPIC_TOKENS) -> str:
    """Human-readable canonical form of the topic fingerprint (also the uniqueness key)."""
    return "-".join(topic_tokens(question, limit))


def classify_url(url: str | None) -> tuple[str, str] | None:
    """Map a URL to the venue worth remembering: ('subreddit', 'r/x') or ('site', 'host').

    Returns None for URLs with no topical venue (bare reddit.com, search engines, shorteners) —
    remembering those would just teach the registry that the internet exists.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if select.is_reddit(host):
        match = _REDDIT_SUB.search(parsed.path or "")
        return ("subreddit", f"r/{match.group(1).lower()}") if match else None
    # Sites are stored as the REGISTRABLE DOMAIN, the same identity select.py groups candidates by.
    # Storing the exact host meant a registry entry for docs.vendor.com could never match the
    # candidate bucket named vendor.com, so site priorities silently did nothing.
    host = select.domain_key(url)
    if not host or host in _IGNORED_HOSTS:
        return None
    return ("site", host)


def rank_rows(rows: list[tuple], wanted: list[str],
              min_overlap: int = MIN_TOPIC_OVERLAP) -> list[tuple[str, float]]:
    """Rank registry rows for a topic. Pure — no DB, no network, so it is directly testable.

    rows: (identifier, useful_runs, useful_hits, total_items, topic_tokens)

    The same identifier can hold several rows (one per past topic), so rows are MERGED per venue
    first — otherwise one subreddit could occupy every returned slot and the caller would issue the
    identical site-scoped search twice. Rows below `min_overlap` shared tokens are dropped: a single
    shared word is not the same subject. Ranking is then overlap, then distinct useful RUNS, then
    hit rate, then absolute hits. Rate before volume on purpose — a venue with 4/5 useful beats one
    with 6/300, which is exactly the generic-subreddit failure this registry exists to stop.
    """
    target = {t.lower() for t in wanted}
    floor = min(min_overlap, len(target)) if target else 0
    merged: dict[str, list[float]] = {}
    for identifier, useful_runs, useful, total, tokens in rows:
        overlap = len(target & {str(t).lower() for t in (tokens or [])})
        if overlap < floor:
            continue
        current = merged.setdefault(str(identifier), [0.0, 0.0, 0.0, 0.0])
        current[0] = max(current[0], float(overlap))
        current[1] += float(useful_runs or 0)
        current[2] += float(useful or 0)
        current[3] += float(total or 0)
    ranked = []
    for identifier, (overlap, runs, useful, total) in merged.items():
        ranked.append((identifier, overlap, runs, (useful / total) if total else 0.0, useful))
    ranked.sort(key=lambda r: (-r[1], -r[2], -r[3], -r[4], r[0]))
    return [(r[0], r[1]) for r in ranked]


def _connect(autocommit: bool = True):
    """Connect with a bounded statement timeout, set as a STATEMENT rather than a startup option.

    `options=-c statement_timeout=...` is rejected outright by Neon's pooled endpoint —
    "unsupported startup parameter in options" — so every registry read and write failed with a
    connection error the moment this ran against production. Fail-soft hid it: the run continued and
    logged a skip, so the engine's new cross-run memory was recording nothing at all while looking
    healthy. Setting it after connecting works on both pooled and direct endpoints, and is itself
    best-effort so a pooler that refuses SET cannot take the registry down either.
    """
    import psycopg

    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=autocommit, connect_timeout=10)
    try:
        conn.execute(f"SET statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
    except Exception as e:
        print(f"[registry] statement_timeout not applied: {type(e).__name__}: {e}", file=sys.stderr)
    return conn


def preferred(question: str, kind: str, limit: int = MAX_REGISTRY_QUERIES,
              min_runs: int = MIN_USEFUL_RUNS) -> list[str]:
    """Venues of `kind` this topic has produced answering evidence from before, best first.

    Fail-soft by design: a registry that is missing, empty, or erroring must degrade the run to
    plain discovery, never break it.
    """
    wanted = topic_tokens(question)
    if not wanted:
        return []
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT identifier, useful_runs, useful_hits, total_items, topic_tokens "
                "FROM vertical_sources "
                "WHERE kind=%s AND useful_runs >= %s AND topic_tokens && %s::text[] "
                # Revocation: exposure raises total_items, so a promoted venue that stops answering
                # falls below the rate floor and loses its priority without anyone intervening.
                "  AND (total_items = 0 OR useful_hits::float / total_items >= %s) "
                # ...and age-out, because the rate floor only bites on the row that keeps being
                # written. A venue nobody has seen in months stops steering discovery.
                "  AND last_seen > now() - make_interval(days => %s) "
                "ORDER BY useful_runs DESC, useful_hits DESC LIMIT %s",
                (kind, min_runs, wanted, MIN_USEFUL_RATE, MAX_AGE_DAYS, LOOKUP_ROW_LIMIT),
            ).fetchall()
    except Exception as e:
        print(f"[registry] lookup skipped: {type(e).__name__}: {e}", file=sys.stderr)
        return []
    return [identifier for identifier, _ in rank_rows(rows, wanted)][:limit]


def aggregate_run(rows: list[tuple]) -> dict[tuple[str, str], tuple[int, int]]:
    """Fold one run's evidence rows into per-venue (total, useful) counts. Pure.

    rows: (url, answers_question, credibility_tier, page_ownership). `answers_question` is NULL when
    extraction failed or was skipped — counted as retrieved but NOT useful, so an extraction outage
    can never read as a venue endorsement. Vendor/marketing tiers and vendor-owned/affiliate pages
    never earn credit (see _CREDIT_TIERS / _BLOCKED_OWNERSHIP).
    """
    out: dict[tuple[str, str], tuple[int, int]] = {}
    for row in rows:
        url, answers = row[0], row[1]
        tier = row[2] if len(row) > 2 else None
        ownership = row[3] if len(row) > 3 else None
        venue = classify_url(url)
        if not venue:
            continue
        credit = 1 if (answers is True
                       and (tier is None or tier in _CREDIT_TIERS)
                       and ownership not in _BLOCKED_OWNERSHIP) else 0
        total, useful = out.get(venue, (0, 0))
        out[venue] = (total + 1, useful + credit)
    return out


def record_run(run_id: int) -> int:
    """Learn from a finished run: record every venue it touched, once, with this run's counts.

    Called AFTER extraction (that is when answers_question exists) and before synthesis. Idempotent:
    the per-run ledger means calling this twice for the same run is a no-op. Returns the number of
    venues newly credited. Never raises — a registry write must not cost a delivered run.
    """
    try:
        with _connect(autocommit=False) as conn:
            question = conn.execute(
                "SELECT question FROM research_runs WHERE run_id=%s", (run_id,)).fetchone()
            if not question:
                return 0
            tokens = topic_tokens(question[0])
            if not tokens:
                return 0
            rows = conn.execute(
                "SELECT url, answers_question, credibility_tier, page_ownership FROM evidence_items "
                "WHERE run_id=%s AND url IS NOT NULL", (run_id,)).fetchall()
            venues = aggregate_run(rows)
            if not venues:
                return 0
            key = "-".join(tokens)
            written = 0
            for (kind, identifier), (total, useful) in sorted(venues.items()):
                vsid = conn.execute(
                    "INSERT INTO vertical_sources (kind, identifier, topic_key, topic_tokens) "
                    "VALUES (%s,%s,%s,%s::text[]) "
                    "ON CONFLICT (kind, identifier, topic_key) DO UPDATE SET last_seen=now() "
                    "RETURNING vertical_source_id",
                    (kind, identifier, key, tokens),
                ).fetchone()[0]
                # The ledger is the idempotency key: counters only move when the run is NEW to this
                # venue, so a retried stage cannot manufacture a promotion.
                claimed = conn.execute(
                    "INSERT INTO vertical_source_runs "
                    "(vertical_source_id, run_id, total_items, useful_items) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (vertical_source_id, run_id) DO NOTHING "
                    "RETURNING vertical_source_id",
                    (vsid, run_id, total, useful),
                ).fetchone()
                if not claimed:
                    continue
                conn.execute(
                    "UPDATE vertical_sources SET times_seen=times_seen+1, "
                    "  total_items=total_items+%s, useful_hits=useful_hits+%s, "
                    "  useful_runs=useful_runs+%s, last_seen=now() "
                    "WHERE vertical_source_id=%s",
                    (total, useful, 1 if useful > 0 else 0, vsid),
                )
                written += 1
            return written
    except Exception as e:
        print(f"[registry] record skipped: {type(e).__name__}: {e}", file=sys.stderr)
        return 0


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="record or inspect the vertical source registry")
    ap.add_argument("--run", type=int, help="record venues for a finished run (idempotent)")
    ap.add_argument("--for-question", help="show the venues this topic has worked in before")
    a = ap.parse_args()
    if a.run:
        print(f"recorded {record_run(a.run)} venues for run {a.run}")
    if a.for_question:
        print("subreddits:", preferred(a.for_question, "subreddit", limit=5))
        print("sites:", preferred(a.for_question, "site", limit=5))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
