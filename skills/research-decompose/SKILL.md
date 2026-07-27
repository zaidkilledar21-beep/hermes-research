# Purpose
Turn an owner's raw research question into sub-questions + a per-sub source plan, then hand off
to collection. Used by the `director` profile.

# Trigger conditions
A new research question arrives (Telegram message to the gateway, or a research_runs row at
status='decomposing').

# Required inputs
- The question text.
- The available sources:
  - Legit (clean, trusted): x, github, youtube, rss, web, hackernews.
  - Primary records (clean, authoritative — the documents an expert cites): sec_edgar (SEC
    filings full-text), courtlistener (court opinions/dockets), fda_enforcement (FDA recall
    records). Pick these whenever a sub-question is regulatory, legal, or about enforcement
    history — they answer with the primary document, not commentary about it.
  - Community/anecdotal (no login, residential proxy; UNTRUSTED but real): reddit_reach,
    stackexchange_reach, trustpilot_reach, forum_reach.
  - Walled burner (login required; only reddit-class is on): instagram_reach, facebook_reach.
  Source-query conventions: `web` takes a PAGE URL; `forum_reach` takes a full thread/search URL;
  `trustpilot_reach` takes a business domain (e.g. `3plguys.com`); all others take a search phrase.

# Workflow
1. Break the question into 1-5 concrete sub-questions using the FACET TEST:
   - Identify the question's independent FACETS — cost, regulation, competitors, reputation,
     logistics, customers, enforcement history... A facet is independent when answering it needs
     DIFFERENT evidence than the others (a licensing database answers regulation; a complaint
     thread answers reputation; neither answers the other).
   - One sub-question per facet. A question with 3+ facets answered by ONE sub-question is
     UNDER-DECOMPOSED — collection budget is per-sub-question, so under-decomposition silently
     starves coverage (measured: 14 campaign runs, every one a single sub-question, and the
     shallowest answers came from exactly the multi-facet questions).
   - Do not pad either: a genuinely single-facet question gets ONE sub-question. 5 is a cap,
     not a target.
2. For each sub-question pick the MINIMAL source set that can answer it. Rules:
   - Match source to CLAIM TYPE, not just topic:
     - Facts / rules / regulation / official status → web (authority pages), rss, github.
     - Lived experience, "does X actually work", vendor reputation, gotchas, "anyone tried" →
       community sources (reddit_reach, stackexchange_reach, hackernews, and forum_reach for a
       known forum thread). This is exactly the signal generic web search buries.
     - Reputation of a specific vendor/product → trustpilot_reach with that vendor's domain.
   - Source lanes that are NARROW on purpose — pick them only for their lane:
     - x: FRESHNESS. Its search covers a recent window, so it answers "what is happening NOW"
       (breaking enforcement news, a live vendor dispute, this week's outage chatter) — it cannot
       answer accumulated-experience questions and returns ~nothing for them. Pair it with a
       community source, never use it alone.
     - stackexchange_reach: technical/laboratory questions (chemistry, lab technique, software) —
       wrong lane for business/commerce questions, where its answer count is ~zero.
     - instagram_reach: a vendor's own marketing presence and influencer promotion — evidence of
       how a product is SOLD (vendor_marketing tier), never of whether it works.
   - A good experiential sub-question usually pairs ONE authority source with ONE community source,
     so synthesis can weigh official claims against practitioner reports.
   - Community/walled = untrusted, contained; use it for anecdote, never as sole proof of a hard fact.
3. Write each sub-question + source_plan to the sub_questions table (or return as JSON for submit).
   (The collector layer auto-adds one experience-focused phrasing for community search sources, so
   you don't need to hand-write "review"/"anyone tried" variants — just pick the right sources.)

# Required output schema
{"sub_questions":[{"text":"...","source_plan":["web","reddit_reach"]}, ...]}

# Prohibited actions
- No collection, no scraping, no model reasoning about answers here — decomposition only.
- Never invent a source not in the available list.

# Escalation
If the question is unanswerable by any available source, return a single sub-question with an
empty source_plan and note the gap; the run will surface "insufficient sources" to the owner.
