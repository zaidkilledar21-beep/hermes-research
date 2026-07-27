"""Query expansion + compression for search-type sources.

Generic web search buries first-person experience under SEO content. For the sources that take a
free-text SEARCH query AND carry community/anecdotal signal, we add one extra phrasing tuned to
surface lived experience ("anyone tried", "review", "problems"). Deterministic, no LLM call.

Search-engine DISCOVERY sources (web_search, reddit_threads) additionally get failure-language
query families — see FAILURE_FAMILIES below for why topic-shaped queries structurally cannot find
the complaint that answers a vendor question.

Sources whose query is a URL (web, forum_reach) or a fixed identifier (trustpilot_reach = a domain)
are NOT expanded — the query means something specific there. Volume stays bounded: at most 2
queries per source, and collectors dedup by content hash, so overlap collapses to a no-op.

compress_for_search() exists because X/GitHub/HN-style search APIs parse the query as structured
syntax, not a sentence — a full research question ("...and being dropped after...") gets read as
boolean operators and hard-fails (X: "Ambiguous use of and as a keyword"). Condenses to short
keywords, once per sub-question (not per source).

Deliberately NOT an LLM call, unlike extract.py's Nemotron pass. Tried it (twice, with an explicit
example and a "keywords only, no preamble" instruction) — the free model kept prepending reasoning
("The user is asking for..."), which is exactly the kind of chatter extraction handles fine on a
long-form cleanup task but that corrupts a task where the ENTIRE OUTPUT must be short and clean.

v3 CORRECTION: that experiment's observation was right but its conclusion overreached. The failure
was the FREE model with NO output constraint — pipeline/plan_queries.py now does query planning on
a paid pinned slug with response_format=json_object (the same discipline synthesize.py uses) and
supersedes the failure families for the pooled discovery sources (web_search, reddit_threads).
This module remains the FAIL-SOFT FLOOR: every planner failure path degrades to variants()
unchanged, and every non-pooled source never left this path at all. Do not delete anything here.
"""
from __future__ import annotations
import re

from pipeline.select import int_env

# Free-text search sources that yield community/practitioner discussion.
_ANECDOTE_SOURCES = {"reddit_reach", "stackexchange_reach", "hackernews"}
# High-volume APIs we deliberately keep to a single query to control item count / cost.
_SINGLE_QUERY = {"x", "github"}

# Sources whose query IS a structured search string, not a URL/domain — these are the ones that
# need compress_for_search(). web/rss/youtube take a URL; trustpilot_reach a domain; forum_reach a
# thread URL — none of those should ever be compressed, they'd stop meaning what they mean.
SEARCH_TYPE = {"x", "github", "hackernews", "reddit_reach", "stackexchange_reach",
               "instagram_reach", "web_search", "reddit_threads",
               # v3 Part H: full-text search APIs — a raw research question would be parsed as
               # (and fail as) a boolean expression there exactly like on X/GitHub.
               "sec_edgar", "courtlistener", "fda_enforcement"}
# reddit_threads belongs here even though it is a reach source: its query goes to a SEARCH ENGINE
# (site:reddit.com discovery) before any thread is read. Feeding it the raw question measurably
# hurt discovery — the full 300-char question found 3 threads where the compressed keywords found 8.

_STOPWORDS = {"and", "or", "not", "the", "a", "an", "what", "do", "does", "is", "are", "of", "to",
             "in", "on", "for", "with", "about", "their", "this", "that", "how", "who", "which",
             "when", "where", "will", "can", "you", "your", "vs", "focus", "real", "other", "any",
             "include", "e.g", "eg", "e-commerce", "orders", "providers", "provider"}

# Pure numbers / percentages / list-enumerators ("1", "99%", "100%", "2.") — these LOOK entity-ish
# to the digit rule but carry NO topical signal, and on GitHub/Reddit search they match millions of
# unrelated results, drowning the real brand terms in noise (root cause of run 11's garbage haul).
_NOISE_TOKEN = re.compile(r"^\d+([.%)]|st|nd|rd|th)?$")


def _is_noise(core: str) -> bool:
    return bool(_NOISE_TOKEN.match(core))


def _is_entity(word: str) -> bool:
    """A token worth keeping FIRST: brand/acronym/domain-shaped — mid-word capital (3PLGuys),
    all-caps acronym (RUO, 3PL), alphanumeric mix (3PL), or looks like a domain (has a dot).
    Pure numbers/percentages are explicitly NOT entities (they match everything, anchor nothing)."""
    core = word.strip(",.;:")
    if not core or _is_noise(core):
        return False
    if "." in core and not core.endswith("."):  # domain-shaped (3plguys.com)
        return True
    if any(c.isdigit() for c in core) and any(c.isalpha() for c in core):  # alnum mix (3PL, BPC157)
        return True
    if core.isupper() and len(core) > 1:  # acronym (RUO, FDA)
        return True
    if re.search(r"[A-Z]", core[1:]):  # mid-word capital (3PLGuys), not just Title Case
        return True
    return False


def compress_for_search(question: str) -> str:
    """Condense a long question into a short, boolean-safe search query — no LLM call (see module
    docstring for why). Entities (brand names, domains, acronyms) are kept first so a long question
    never drops its actual subject just because it appears late in the sentence. Pure numeric noise
    tokens are dropped entirely."""
    if not question.strip():
        return question
    # Em/en dashes and a bare hyphen are SYNTAX to search APIs, not punctuation: X rejects the whole
    # query ("400 Bad Request") when an em-dash survives compression, and a leading '-' is X's NOT
    # operator, which silently inverts a term instead of failing loudly. Both cost the entire source
    # for that sub-question. Intra-word hyphens are kept — BPC-157 and third-party are real tokens.
    cleaned = re.sub(r'[()?"/]', " ", question)
    cleaned = re.sub(r"[‐-―−]", " ", cleaned)     # ‐ ‑ ‒ – — ― and −
    raw = [w.strip(",.;:").lstrip("-") for w in cleaned.split()]
    words = [w for w in raw if w and w.lower() not in _STOPWORDS and not _is_noise(w.strip(",.;:"))]
    entities, rest = [], []
    for w in words:
        (entities if _is_entity(w) else rest).append(w)
    out, seen = [], set()
    for w in entities + rest:
        key = w.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(w)
        if len(out) >= 8:
            break
    return " ".join(out)


_DOMAIN_RE = re.compile(r"\b([a-z0-9][a-z0-9-]*\.(?:com|net|org|io|co|us|shop|store|xyz))\b", re.I)


def extract_domain(text: str) -> str | None:
    """Pull the first domain-looking token out of a question — trustpilot_reach needs a bare domain
    (e.g. '3plguys.com') as its query, NOT the full sentence (which yields a 'No results' search)."""
    m = _DOMAIN_RE.search(text or "")
    return m.group(1).lower() if m else None


# ---------------------------------------------------------------------------------------------
# Failure-language query families
# ---------------------------------------------------------------------------------------------
# WHY: a topic-shaped query ("<vendor> 3PL fulfillment peptides") retrieves the vendor's own
# marketing and generic industry chatter, because that is what the topic words index against. The
# evidence that actually decides a vendor question is the COMPLAINT — and complaints are written in
# failure language ("they froze my reserve", "account terminated", "inventory went missing"), not
# topic language. Run 28 proved retrieval volume was already solved and aim was not: 166 items were
# retrieved and the relevance filter correctly rejected 156, because the threads discovered were
# generic r/logistics chatter rather than operators reporting a specific failure.
#
# Terms are grouped into FAMILIES rather than kept as one flat list because each extra query costs
# real discovery budget downstream (every Reddit thread read is a browser render on a 4GB box). One
# representative term per family means a tight cap still spans four DIFFERENT failure modes instead
# of four synonyms for "scam". Deterministic and code-only — no model call, same reason as
# compress_for_search (see module docstring).
FAILURE_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("funds",      ("reserve", "frozen", "chargeback", "withheld payment")),
    ("account",    ("terminated", "banned", "suspended", "shut down")),
    ("inventory",  ("missing inventory", "lost inventory", "damaged")),
    ("reputation", ('"do not use"', "scam", "avoid", "complaints")),
)

# Sources whose query goes to a SEARCH ENGINE and whose value is finding the complaint — these get
# the failure families. Deliberately NOT x/github (kept single-query for cost) and not any source
# whose query is a URL or a bare domain.
_FAILURE_SOURCES = {"web_search", "reddit_threads"}

# Total failure queries per (source, sub-question). 4 = one per family for the primary alias.
FAILURE_QUERY_CAP = int_env("FAILURE_QUERY_CAP", 4)
# How many vendor aliases to hang failure language on. 2 covers "3plguys" + "3PLGuys.com" style
# duplicates without multiplying the discovery budget.
MAX_ALIASES = int_env("FAILURE_MAX_ALIASES", 2)
# Brandless fallback anchor width (see aliases()/_fallback_anchor).
_FALLBACK_ANCHOR_TOKENS = 3

_TLD_SUFFIX = re.compile(r"\.(?:com|net|org|io|co|us|shop|store|xyz|ai|app)$", re.I)


def aliases(text: str, limit: int = MAX_ALIASES) -> list[str]:
    """Vendor-ish anchors to hang failure language on, best first.

    An anchor is what makes a failure query specific: "reserve frozen" alone matches every payments
    complaint on the internet, while "3plguys reserve" matches the one that answers the question.
    Anchors are the entity-shaped tokens compress_for_search already ranks first (brands, domains,
    acronyms); a domain is reduced to its bare name because forum posts say "3plguys", not
    "3plguys.com". Case is preserved (search engines are case-insensitive, humans reading the query
    log are not); dedup is case-insensitive.
    """
    seen: set[str] = set()
    out: list[str] = []
    for word in re.sub(r'[()?"/]', " ", text or "").split():
        core = word.strip(",.;:")
        if not core or not _is_entity(core):
            continue
        anchor = _TLD_SUFFIX.sub("", core)
        key = anchor.lower()
        if not anchor or key in seen:
            continue
        seen.add(key)
        out.append(anchor)
        if len(out) >= limit:
            break
    return out


def _fallback_anchor(text: str) -> str | None:
    """Anchor for a question that names no brand at all ("what goes wrong with peptide 3PLs").

    Failure language is still worth asking for there — it just has to be anchored on the topic's
    own keywords instead of a vendor name. Requires at least 2 content tokens: anchoring "scam" on
    a single word retrieves noise, not evidence.
    """
    tokens = [w.strip(",.;:") for w in re.sub(r'[()?"/]', " ", text or "").split()]
    tokens = [w for w in tokens if w and w.lower() not in _STOPWORDS and not _is_noise(w)]
    if len(tokens) < 2:
        return None
    return " ".join(tokens[:_FALLBACK_ANCHOR_TOKENS])


def family_term(terms: tuple[str, ...], text: str) -> str:
    """Pick which word of a family to actually search for.

    If the question already names one of the family's words, use THAT one — a question about
    "damaged, temperature-spoiled inventory" should search "damaged", not the family's default
    "missing inventory", which describes a different failure. Falls back to the family's leading
    term, which is the most common phrasing of that complaint.
    """
    lowered = (text or "").lower()
    for term in terms:
        bare = term.strip('"').lower()
        # Word boundaries, not substrings: "undamaged" contains "damaged", "preserve" contains
        # "reserve", and "scampi" contains "scam" — each would have flipped the family to a term
        # the question is actually negating or never mentioned.
        if bare and re.search(rf"\b{re.escape(bare)}\b", lowered):
            return term
    return terms[0]


def failure_variants(text: str, cap: int = FAILURE_QUERY_CAP) -> list[str]:
    """Failure-language queries for a sub-question, capped. Empty when nothing can be anchored.

    Ordering is anchor-major / family-minor ON PURPOSE: with a cap of 4 the primary anchor gets one
    query per failure mode (funds, account, inventory, reputation) before a second alias repeats a
    mode. Breadth of failure modes beats breadth of aliases — the aliases are usually the same
    vendor spelled two ways, while the families are genuinely different complaints.
    """
    if cap <= 0:
        return []
    anchors = aliases(text) or [a for a in (_fallback_anchor(text),) if a]
    out: list[str] = []
    for anchor in anchors:
        for _family, terms in FAILURE_FAMILIES:
            out.append(f"{anchor} {family_term(terms, text)}")
            if len(out) >= cap:
                return out
    return out


def variants(source_id: str, text: str) -> list[str]:
    """Return the list of queries to actually run for this (source, sub-question).

    Length 1 for URL/domain/single-query sources, 2 for anecdote sources, and up to
    1 + FAILURE_QUERY_CAP for the search-engine discovery sources. Callers that fan out to an
    expensive read (web_search, reddit_threads) must POOL the results of these queries and select a
    diverse subset (pipeline/select.py) rather than reading every query's hits — otherwise the extra
    aim multiplies the read budget instead of sharpening it.
    """
    text = (text or "").strip()
    if not text:
        return [text]
    if source_id in _FAILURE_SOURCES:
        # Deduped: a question that already reads "PCAC reserve" would otherwise emit that query
        # twice and pay for the same search twice.
        return list(dict.fromkeys([text, *failure_variants(text)]))
    if source_id in _ANECDOTE_SOURCES:
        anecdote = f'{text} experience review "anyone tried" problems'
        return [text, anecdote]
    # URL/domain/single-query sources: use the query verbatim.
    return [text]
