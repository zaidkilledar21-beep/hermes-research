# Todo: checkable backlog

## Done
- [x] VPS foundation (Docker, swap, users, firewall, reboot)
- [x] Neon DB + schema (research_runs, sub_questions, sources, evidence_items, findings, agent_runs,
      reviews; report_md column added)
- [x] Legit collectors built. GitHub proven live (25 real issues, real synthesis)
- [x] Model stack researched, locked, wired: Hy3 (director), MiniMax M3 (synthesis), Nemotron free
      (bulk), Codex Terra + Claude Opus 5 (reviewers; upgraded from Opus 4.8 on 2026-07-24), all
      pinned exact slugs, not aliases
- [x] Mission Control web console. Overview/Research/Chat/Services/Cost, dark theme, all live
- [x] Cloudflare Tunnel (research.example.com + chat.example.com) + Cloudflare Access single-login
      gate; app-level password removed once Access confirmed working
- [x] Hermes container live, chat working end-to-end (websocket fixed via Caddy loopback shim)
- [x] `run-research` skill wired. Hermes chat can trigger the pipeline via /api/ask + /api/run
- [x] Reviewer container live, Codex + Claude both authenticated, models locked permanently
- [x] Camoufox pivot. Reach container rebuilt after agent-reach/OpenCLI/rdt-cli dead-ended
- [x] Residential proxy (DataImpulse) wired, country-pinned
- [x] Reddit walled source. Working, no account needed
- [x] Instagram walled source. Working, burner session live and verified
- [x] X. Working via paid API ($5 minimum confirmed, bearer token wired, 10 real posts verified live)

## Nuance upgrade (2026-07-24): code done locally, NOT yet deployed
- [x] Credibility tiering: `credibility_tier` on `sources` + `evidence_items` (claim-class, orthogonal
      to A/B/C grade). Resolved at ingest in `common.store_evidence` (source baseline + web-domain
      authority heuristic). Migration: `db/migration_002_credibility_tier.sql`.
- [x] New community/anecdote sources: `hackernews` (legit API), `stackexchange_reach`,
      `trustpilot_reach`, `forum_reach` (generic thread reader) added to `reach_camoufox.py`.
- [x] Synthesis upgrade: new `community_signal` finding label + explicit contradiction linking
      (wires the dormant `findings.contradicts`); model now sees credibility_tier per item.
- [x] Query expansion (`pipeline/queries.py`): community search sources get a 2nd experience-focused
      phrasing automatically.
- [x] Skills updated: `run-research` (community sources ON, IG/FB still OFF) + `research-decompose`
      (source-by-claim-type guidance). Web UI: source pills + report renderer show tier + community group.
- [x] **DEPLOYED (2026-07-24)**. Migration_002 applied to Neon; collectors/pipeline/web synced to
      the host checkout + uvicorn restarted; run-research skill synced into the hermes container +
      hermes restarted. Web app HTTP 200 on the new code.
- [x] **Reach image rebuilt**. The 3 new readers all smoke-tested live end-to-end:
      stackexchange (switched to the official SE API, SO search is Cloudflare-walled),
      trustpilot (extracts embedded `__NEXT_DATA__` reviews. Got real +/- reviews for 3plguys.com),
      forum (generic thread reader, challenge-wait added). `_harvest` now waits past Cloudflare/JS
      interstitials instead of returning the placeholder.
- [x] **All sources enabled in the UI except Facebook**. X pill added (was missing entirely),
      everything pre-checked except facebook_reach (burner suspended). Skill updated to match
      (X + Instagram + Stack Exchange usable; only Facebook off).
- [x] **Nemotron bulk-extraction wired + live (2026-07-24)**. Free `nvidia/nemotron-3-ultra-550b-
      a55b:free` now runs as a real pipeline stage (`pipeline/extract.py`) between collection and
      synthesis. Cleans every raw item into dense, claim-preserving text (strips site chrome, keeps
      claims VERBATIM), concurrent (8 workers), fail-soft. New `evidence_items.extracted` column
      (migration_003); `content` stays immutable. Because extraction condenses, raised collection
      40/source and synthesis evidence ceiling 20→60. Also a cheap regex chrome-strip in the reach
      `_harvest` before storage. Live-verified: Nemotron stripped nav junk, kept a forum claim +
      username + "$8000/6 months" verbatim, cost $0.
- [x] **Web search added + proven (2026-07-24)**. Self-hosted SearXNG container
      (`deploy/searxng-run.sh`, 127.0.0.1:8888, $0, no key, in crontab @reboot) + `web_search` legit
      collector (SearXNG finds URLs → Jina reads them). Now the lead ALWAYS_SOURCE. This was the real
      missing piece. Run 14 (3PLGuys 3PL question) went from anime/K-pop garbage to on-topic 3PL
      pages + real Trustpilot reviews + marketing-vs-review ⚔ contradictions + honest gaps, $0.0033.
- [ ] Reddit-as-direct-source is weak: `.json` API hard-blocked (lessons.md #14), HTML scrape
      relevance is mediocre. Not urgent: web_search surfaces relevant Reddit threads via Google index.
      If ever needed, route reddit through SearXNG `site:reddit.com` host-side instead of reach scrape.
- [ ] Trustpilot intermittency: `__NEXT_DATA__` extraction sometimes blocked by Cloudflare. Worked
      run 14 (7 reviews), returned 0 on an earlier run. Add a retry / challenge-wait if it recurs.
- [x] **Cross-run synthesizer BUILT + proven (2026-07-24)**. This WAS the deferred "staged/chained
      runs" item, delivered as an on-demand consolidator. New `cross_syntheses` table
      (migration_005), `pipeline/cross_synthesize.py` (host bridge → reviewer dropbox),
      `reviewer/reviewer_agent.py` `kind="cross_synthesis"` branch running the **two-model synth+critic
      chain** (Claude Opus draft → Codex `gpt-5.6-terra` adversarial critique → Claude revise, all via
      the reviewer container's subscription auth = $0), `/api/synthesize` + `/api/synthesis/{id}` +
      `/api/runs` endpoints, and the `synthesize-project` Hermes skill. Proven: synthesis 1
      consolidated runs 14/15/18 into an 18k-char brief; Codex caught the draft claiming runs 14+15
      agreed when they contradicted, forcing a materially more careful revision. $0.00.
      Owner directive confirmed: Claude STAYS at the per-finding gate (not redundant with Codex there);
      this ADDED a role. Model tiers are a free one-line A/B knob.
- [x] **Confidence-display fix (2026-07-24)**: `observed` findings no longer render "(confidence
      0.00)"; confidence is now stored only for inferred/community_signal (synthesize.py). Applies
      to runs going forward.
- [ ] Cross-synthesis is a long sequential job in the reviewer loop. If it ever contends with
      per-finding reviews, split to a separate queue/worker (not needed at current volume).

## Retrieval + reliability v2: DONE 2026-07-25 (built with Codex in tandem)
- [x] Synthesis failure taxonomy + bounded repair retry; parse failure can no longer report as
      "no findings" (migration_007). 12 unit tests.
- [x] Granular release gate: per-finding `disposition`, withheld findings listed in the report,
      systemic-only blocking. One bad finding no longer destroys a report.
- [x] Fabricated evidence ids reach the gate instead of being silently stripped.
- [x] `community_signal` findings are now REVIEWED; reviewers read the same text synthesis used;
      review prompts are label-aware so anecdote isn't rejected for being anecdotal.
- [x] Comment-level `reddit_threads` reader (migration_008): author/thread/comment provenance,
      top+new+controversial rotation, one browser session. Verified live. Run 28 pulled **166
      evidence items from 121 distinct authors across 8 threads** (was 8 items, 0 authors).
- [x] Relevance-first evidence selection (`answers_question`/`facet` from extraction) replaces
      ORDER BY grade, which had let 40 irrelevant GitHub repos bury every web result.
- [x] Completion-tracked waits for reach + reviewers. Fixed sleeps were orphaning real results
      (lessons #17). Retired Reddit's own search entirely.
- [x] Pushed to a private GitHub repo (secrets scanned, `.collab` transcripts excluded).

## Discovery targeting (v2 completion): CODE DONE 2026-07-25, NOT YET DEPLOYED
- [x] **Failure-language query families** (`pipeline/queries.py`). Four semantic families (funds /
      account / inventory / reputation), one representative term each, anchored on vendor aliases
      extracted from the question (domains reduced to bare names: `3plguys.com` -> `3plguys`).
      `family_term()` searches the word the QUESTION uses when it uses one ("damaged", not the
      family default "missing inventory"), word-boundary matched so "undamaged"/"preserve" don't
      trigger it. Brandless questions fall back to a topic anchor. Capped at 4, deterministic, $0.
- [x] **Diversity quotas + per-venue caps** (`pipeline/select.py`, new). Discovery pools candidates
      per query and INTERLEAVES them (concatenating put every base-query hit ahead of every
      failure-query hit, so the extra searches would have cost time and changed nothing). Three
      tiers with reserved floors, plain base query (50%), registry-scoped, expansions, so
      complaint expansion cannot displace `fda.gov` on a regulatory question. Per-venue caps
      (2/domain, 3/subreddit), subdomains collapsed to the registrable domain, Reddit host aliases
      and tracking params canonicalized so one thread is never rendered twice.
- [x] **Vertical source registry** (`pipeline/registry.py`, migration_009). Cross-run memory of
      which subreddits/hosts produced evidence the extractor judged `answers_question`. Four
      defences against self-reinforcement: promotion by DISTINCT RUN (not item), revocable via a
      hit-rate floor + 120-day age-out, per-run ledger for idempotency, and no credit for
      vendor-marketing tiers or vendor-owned/affiliate/sponsored pages. It only reorders venues
      discovery already found and adds <=2 site-scoped queries, never on the citation path.
- [x] **Per-source / per-thread caps in synthesis** (`pipeline/synthesize.py`). 25/source, 6/thread,
      applied in SEQUENCE (thread first, then source). Computing both ranks independently and
      AND-ing them underfilled the budget. Regression-tested against sqlite: the old query returned
      2 items where the budget was 4.
- [x] **Run-wide budget ceilings** (`run._Budget`): 40 searches / 24 web reads / 16 threads for the
      WHOLE run, refunded when a facet discovers nothing. Previously every sub-question got a fresh
      allowance, so run cost scaled with however many facets the director invented.
- [x] **Judged eval set** (`evals/`, 10 hand-labelled questions + deterministic scorer). Never
      starts a research run: plans queries, asks SearXNG, applies the production selection policy,
      scores what WOULD have been read. `--plan-only` needs no DB, no network. Legacy plan scores
      mean aim 0.85, current 1.00, four named questions move.
- [x] **Three adversarial Codex review rounds**: 16 + 13 + 4 defects found and fixed (see
      lessons.md #19). 145 unit tests, all passing.
- [x] **DEPLOYED 2026-07-25**. Migration_009 applied to Neon (both tables + 3 indexes verified,
      0 rows); `pipeline/`, `collectors/`, `evals/`, `tests/` synced to `~/hermes-build`; uvicorn
      restarted via the watchdog, HTTP 200; 145 tests pass ON THE BOX (Python 3.14.4).
- [x] **SearXNG routed through the residential proxy (2026-07-25, owner-approved).** The datacenter
      IP was the root cause of the engine suspensions. SearXNG now dials out through the same
      DataImpulse proxy reach uses, with its OWN exit country (`SEARXNG_PROXY_COUNTRY=us`) so results
      are not localized to reach's `br` pin (that pin exists for Instagram session consistency and
      must not change). Rendering moved out of `sed` into `deploy/render-searxng-settings.py`:
      the proxy URL never enters a process argv (`/proc/<pid>/cmdline` is world-readable), only the
      five proxy keys are read from `.env` instead of sourcing every secret in it, credentials are
      URL-quoted, and an empty password is refused. The launcher health-checks the JSON endpoint
      after starting and rolls back to the previous settings if the new render does not serve.
- [x] **Throttle detection** (found by the first live eval, fixed same session). SearXNG's upstream
      engines suspend under the new ~5 queries/sub-question load (`google cse: unusual traffic`,
      `duckduckgo 403 suspended_time=180`) and answer 200 with an empty result list, which is
      indistinguishable from "nothing was written about this". Now: `unresponsive_engines` is read
      from the response, one backoff-retry, 1.5s process-wide pacing, a counter, and a note on
      `research_runs.notes` so a throttled run is never reported as an empty one. Lessons #22.
- [ ] Re-run the PCAC question (run 20 died to the parse bug that is now fixed). Held deliberately:
      owner asked for no research runs beyond what testing requires.

## Open
- [ ] **Facebook**. Burner suspended on creation, appeal pending. No action until/unless appeal
      resolves; not blocking anything else.
- [ ] **Burner IG password not saved**, only email/email-password are in `.burner-creds.env`; the
      actual Instagram password was used once and not captured. Decide whether to retroactively save
      it (needed only if the saved session ever fully expires and Hermes needs to re-login itself).
- [ ] **Resource check**. Run `docker stats` under a real concurrent load (web app + Hermes + reviewer
      + reach) and confirm it's still inside a sane budget on the 4GB box; nothing catastrophic
      expected but never actually measured with the full current service set.
- [ ] **Kill-switch drill**, never tested end-to-end. Confirm it actually stops in-flight work and
      preserves data as designed.
- [ ] **Injection-resistance re-verify**. Proven once on synthetic data early in the build, before the
      reviewer layer and Camoufox sources existed. Worth one more pass with real walled-source content.
- [ ] **Full E2E with a walled source**, no single run has gone submit → reach collection → synthesis
      → reviewers → gate → report in one chain yet. Each stage proven individually; chain them once
      for real confidence.
- [ ] Smoke-test the `web`, `rss`, `youtube` legit collectors individually with real queries, only
      `github` has been proven live so far.
