---
name: run-research
description: Run a rigorous, cited research query on any topic using the read-only research engine and return the findings. Use whenever the user wants sourced, evidence-backed answers about what people say, report, or discuss.
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [research, web, github, evidence]
    category: research
    required_environment_variables:
      - RESEARCH_API_USER
      - RESEARCH_API_PASS
---
# Research Engine

## When to use
When the user asks you to research, investigate, find out, look into, or gather evidence about any
topic — "what are people saying about X", "what do developers report about Y", competitor/market
questions, or anything needing current, sourced, cited findings. Prefer this over answering from
memory whenever the user wants evidence-backed information.

## What it does
Drives a read-only research pipeline that collects real sources — official/legit (X, GitHub issues,
web pages, RSS, YouTube, Hacker News) AND community/anecdotal (Reddit, Stack Exchange, Trustpilot
reviews, forum threads, Instagram) — synthesizes them into evidence-linked findings labelled
observed / inferred / community-signal / gap, weighs authority against lived experience, flags
contradictions, runs a deterministic integrity gate, and returns a cited report. It never sends,
posts, or publishes — pure research. Cost is ~1-2 cents per run.

## Procedure — use ONLY the curl calls below for research. See Prohibited section — this is not
optional guidance, it's a hard boundary.
1. Sources — you do NOT need to pick a subset. Every run automatically goes through the FULL
   free-text-search baseline (x, github, hackernews, reddit_reach, stackexchange_reach,
   instagram_reach) — this is enforced server-side (`/api/ask`), not something you have to request.
   `web`/`rss`/`youtube` are NOT in the baseline (they take a URL/feed, not a question) — only add
   them if you have an actual URL. You only ever need to add sources when the question names
   something specific enough to point a shape-specific reader at:
   - Reputation of a NAMED vendor/product → also add `trustpilot_reach` and pass that vendor's
     domain as part of `question` (the pipeline extracts it) — e.g. mention "3plguys.com".
   - A SPECIFIC forum thread you already have the URL for → also add `forum_reach`.
   - `facebook_reach` — never use, burner suspended, non-functional.
   If neither applies, just submit sources empty (or omit the field) — the full baseline still runs.
2. Submit the run:
   ```
   curl -s -u "$RESEARCH_API_USER:$RESEARCH_API_PASS" -X POST \
     http://localhost:8080/api/ask \
     --data-urlencode "question=<the user's exact research question>"
   ```
   Only add `--data-urlencode "sources=trustpilot_reach"` or `"sources=forum_reach"` on top when
   step 1's specific conditions apply — the baseline is already included either way.
   Response JSON: `{"run_id": N, "status": "started", "poll": "/api/run/N"}`. Keep the run_id.
3. Poll roughly every 15 seconds until `done` is true (usually 30–90s), up to ~5 minutes:
   ```
   curl -s -u "$RESEARCH_API_USER:$RESEARCH_API_PASS" http://localhost:8080/api/run/N
   ```
   Response JSON: `{"status": ..., "done": bool, "report_md": ..., "notes": ...}`.
4. When `done` is true:
   - status `delivered` → present `report_md` to the user verbatim (it is markdown: cited findings
     grouped observed / inferred / community-signal / gaps, each with source links, credibility
     tier, and grade; contradictions are flagged with ⚔). Community-signal findings are real but
     anecdotal/low-N — present them as such, don't upgrade them to fact.
   - status `gated` → tell the user the run was blocked by the integrity gate and quote `notes`.
5. If still running after ~5 minutes, tell the user it's taking longer than usual and give them the
   run_id and the link `https://research.example.com/run/N` to check later.

## Prohibited
- **NEVER independently fetch, browse, or scrape web content yourself** (curl to arbitrary sites,
  Terminal/shell HTTP requests, search engines like Bing/Brave, Wayback Machine, or any tool other
  than the curl calls documented above) — not even "to help while the engine runs" or "because it's
  slow." You have NO stealth fingerprinting, NO proxy, and NO evidence pipeline when you do this —
  every raw request you make gets bot-blocked (Bing challenge pages, empty/blocked responses) and
  produces nothing usable, while wasting time and confusing the user with noise they never asked for.
  If the engine is slow, POLL AND WAIT — do not supplement it. The ONLY sanctioned way to gather
  research evidence is the documented `/api/ask` + poll workflow. This applies even if you believe
  you're being extra helpful — freelance scraping is strictly worse than waiting, on every axis.
- Never invent findings, claims, or sources. Relay only what `report_md` contains.
- Never claim a finding the engine did not return.
- Never present a community-signal / untrusted-source finding as established fact — keep the engine's
  hedge ("some users report...") intact.
- The engine only ever READS. Never attempt to post, send, DM, or otherwise act on any platform.
- Never use `facebook_reach` — burner suspended, source is non-functional, not just discouraged.
