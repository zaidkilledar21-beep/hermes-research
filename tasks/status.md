# Status — read this first in a new session

**Last updated: 2026-07-27.** Pick this up cold: read this → `tasks/handoff.md` (what's next) →
`tasks/lessons.md` (don't repeat these) → `tasks/todo.md` (checkable backlog).

> **2026-07-27 — v3 BUILT + DEPLOYED: closing the gap to expert-level research.** Driven by owner
> feedback that the peptide business plan read "extremely generic". Diagnosis was measured, not
> vibed: a new SQL-only capability scorecard (`evals/capability_metrics.py`, baseline frozen in
> `evals/baseline-capability.json`) showed aim 0.204, extraction liveness 8/14, subs_per_run 1.0,
> and **brief retention 0.244** — the consolidated brief kept 98 of the 401 named entities its own
> accepted findings carried. The models were never the cause; the gaps were architectural.
> Ten parts, all committed, all deployed to the box, migrations 010–014 applied to Neon:
> - **Query planner** (`pipeline/plan_queries.py`, Hy3 pinned): per-sub-question retrieval plans —
>   anchors, domain vocabulary, intent-tagged queries, expected_evidence threaded into extraction.
>   STRICT SUPERSET of the deterministic path (measured: replacing the failure families cost aim
>   1.00→0.875; adding kept 1.00 and raised novelty 0.315→0.438). Fail-soft to queries.variants()
>   with plan_state recorded. The offline eval caught two real defects for ~$0.005 (lessons #30).
> - **Brief de-genericized** (reviewer prompts): named-specifics section + hard no-category rule +
>   `lost_specifics` critic dimension. Verified at $0 by re-consolidating runs 29–42 (synthesis 6):
>   **brief retention 0.244 → 0.838** (336/401 entities), 25,338 → 63,329 chars.
> - **Revision loop** (`pipeline/revise.py`): rejected findings get one batched defend-or-revise
>   pass — defence requires a verbatim quote or demotes to drop; originals become
>   `superseded_by_revision` lineage the gate/report/packet all treat as invisible.
> - **Gap-driven iteration** (`pipeline/followup.py`): unknowns + unresolved contradictions spawn
>   ≤3 follow-up sub-questions (contradictions aim at a THIRD source class), collected on a halved
>   budget slice, re-synthesized over ALL evidence, convergence-gated (`deepening` status).
> - **Figures + quote anchoring**: synthesis emits structured figures and verbatim quotes; >3×
>   spreads become reviewable conflict findings; the gate string-matches quotes against cited text
>   (`quarantined_unanchored_quote`). Reports open with a PRISMA-style screening ledger.
>   `model-economics` Hermes skill builds cited unit-economics via code_execution.
> - **Priors** (`pipeline/priors.py` + `vertical_facts`): registry pattern extended from venues to
>   figures — ≥5 prior observations per (vertical, subject, unit), >3× deviation from the median
>   flags a reviewable "surprising vs prior research" finding. Never auto-rejects; cold-start silent
>   and says so.
> - **Primary sources**: `sec_edgar`, `courtlistener`, `fda_enforcement` collectors — free, keyless,
>   grade A `primary_authority`, registered everywhere per lessons #16.
> - **Decomposition + dormant sources**: facet test in the decompose skill (all 14 campaign runs ran
>   on ONE sub-question — the submit.py seed the director never replaced, now named in notes);
>   forum-shaped web hits route to the browser reader (FORUM_REACH_CAP=4); X repositioned honestly
>   as freshness-only (its /search/recent covers ~7 days — the reason it produced 2 items in 14
>   runs) with an X_SEARCH_ARCHIVE switch pending a tier probe; planner short-queries fix its
>   8-term-AND problem.
> - **Model bakeoffs (Part D): both measured, both KEEP.** Extraction fallback qwen-30b was 2×
>   cheaper but systematically more permissive on answers_question (7/37 disagreements, all one
>   direction) — kept deepseek-v4-flash. Synthesis GLM-5.2 doubled finding count with excellent
>   quote discipline (19/20 grounded) but LOWER entity density (17 vs 26) — kept MiniMax M3.
>   Total bakeoff spend ≈ $0.12.
> **Acceptance run 43** (run 35's question — the campaign's worst: 186 evidence → 3 findings)
> launched with all flags on (PLANNER_ENABLED=1, MAX_REVISION_ROUNDS=1, FOLLOWUP_ENABLED=1);
> planner `planned` on Hy3, superset queries visible in the log. After it verifies, ALL further
> research runs are owner-triggered via Hermes chat. 238 tests green locally and on the box.

> **2026-07-25 — DISCOVERY TARGETING built (v2 completion). CODE COMPLETE, NOT DEPLOYED.**
> Run 28 proved volume was solved and aim was not: 166 evidence items retrieved, 156 correctly
> rejected, because topic-shaped queries find the vendor's marketing and generic industry chatter
> rather than the operator complaint that answers the question. Four changes close that:
> - **Failure-language families** (`pipeline/queries.py`): four semantic families (funds / account /
>   inventory / reputation) anchored on vendor aliases pulled from the question. Searches the word
>   the QUESTION uses when it uses one ("damaged", not "missing inventory"). Deterministic, $0.
> - **Diversity selection** (`pipeline/select.py`, new): pooled candidates INTERLEAVED across
>   queries, then three tiers with reserved floors (plain base 50%, registry-scoped, expansions) and
>   per-venue caps. A regulatory question keeps its `fda.gov` sources even though complaint
>   expansion is unconditional.
> - **Vertical source registry** (`pipeline/registry.py`, migration_009): the engine's FIRST
>   cross-run memory — which subreddits/hosts have produced evidence the extractor judged relevant.
>   Promotion is by distinct run, revocable (hit-rate floor + 120-day age-out), ledgered for
>   idempotency, and blind to vendor-owned pages. It reorders venues discovery already found; it
>   never touches the citation path.
> - **Judged eval set** (`evals/`): 10 hand-labelled questions, deterministic scorer, discovery-only
>   — never starts a research run, never calls a paid model, `--plan-only` needs no database at all.
>   Legacy plan mean aim 0.85 → current 1.00. This is the first time a retrieval change here can be
>   argued from a number instead of a good-looking run.
> Also: run-wide budget ceilings (40 searches / 24 web reads / 16 threads), and per-source (25) and
> per-thread (6) caps inside the synthesis evidence budget, applied in sequence — the naive version
> underfilled, returning 2 items where 25 were budgeted.
> **Three adversarial Codex review rounds: 16 → 13 → 4 defects, all fixed.** 145 unit tests pass,
> including a sqlite regression test that runs the production evidence SQL.
>
> **DEPLOYED the same day.** migration_009 applied to Neon (two tables, three indexes, 0 rows), code
> synced to `~/hermes-build`, uvicorn restarted via the watchdog (HTTP 200), full suite green on the
> box under Python 3.14.4. The first live eval paid for itself immediately: one question returned
> zero candidates from five queries, and the cause was not the query — SearXNG's upstream engines had
> suspended us (`google cse: unusual traffic`, `duckduckgo 403 suspended_time=180`) under the new
> ~5-queries-per-sub-question load, answering 200 with an empty result list that is indistinguishable
> from "nothing was ever written about this". Same failure class as run 20's parse error reporting
> itself as "no findings". Now detected via `unresponsive_engines`, retried once after a backoff,
> paced 1.5s apart, and recorded on `research_runs.notes` (lessons #22). Throttling is now VISIBLE
> and survivable — NOT absent: the box's datacenter IP still gets flagged within a few dozen queries,
> and the real fix (routing SearXNG through the residential proxy reach already uses) is a cost
> decision left to the owner.

> **2026-07-24 — nuance upgrade BUILT + DEPLOYED + smoke-tested.** Turned the engine from graded web
> search toward "Perplexity-with-anecdote": credibility tiering (claim-class tag orthogonal to A/B/C
> grade), community/anecdote sources (Hacker News API + Stack Exchange API + Trustpilot + generic-forum
> reach readers), a `community_signal` finding label, explicit contradiction linking, and automatic
> experience-focused query expansion. Migration_002 applied to Neon; all code synced to the VPS +
> services restarted; reach image rebuilt and all 3 new readers verified live (Trustpilot pulled real
> +/- reviews for 3plguys.com via `__NEXT_DATA__`; SE via official API since SO search is Cloudflare-
> walled). **13 sources; all usable except Facebook** (burner suspended). X pill was missing from the
> UI entirely — added.
>
> **Nemotron bulk extraction is now LOAD-BEARING** (was a name in a doc, nowhere in code). New stage
> `pipeline/extract.py` runs the free Nemotron slug over every raw evidence item between collection
> and synthesis — strips site chrome, keeps claims verbatim, concurrent + fail-soft — writing a new
> immutable-safe `evidence_items.extracted` column. Because it condenses, collection is now 40/source
> and synthesis considers up to 60 evidence items (was 20). Pipeline status flow now:
> decomposing → collecting → extracting → synthesizing → reviewing → gated/delivered.
> Only remaining deferred item: staged/chained runs (todo.md).
>
> **2026-07-25 — RETRIEVAL + RELIABILITY v2 (built with Codex in tandem, post adversarial review).**
> A 6-run batch delivered only 3 of 6, and a Codex code review found structural defects. Fixed, in
> priority order (correctness → capability → depth):
> - **Failure semantics.** `synthesis_state` (ok | valid_empty | parse_failed | truncated |
>   schema_invalid | transport_failed) + raw response stored + one bounded repair retry. A parse
>   failure can no longer masquerade as "no findings" — run 20 discarded 5,197 chars of completed
>   analysis and reported it as a negative result.
> - **Granular gate.** Per-finding `disposition` (accepted / quarantined_* / rejected_by_reviewer).
>   One malformed finding no longer nukes a whole report (run 25 lost ~19 good findings that way).
>   Withheld findings are LISTED in the report, never silently dropped. Systemic failures still block.
> - **Fabricated citations reach the gate.** Synthesis stored only *valid* evidence ids, so the gate's
>   fabrication check was dead code AND full fabrication became an unexplained empty-citation finding.
> - **community_signal is now REVIEWED** (was excluded — the most fragile class had zero scrutiny),
>   reviewers now read the SAME text synthesis used (`COALESCE(extracted, content)`, was raw), and
>   review prompts are label-aware so anecdote isn't auto-rejected for being anecdotal.
> - **Comment-level community evidence.** `reddit_threads`: SearXNG `site:reddit.com` discovery →
>   Camoufox reads old.reddit threads → one record per POST/COMMENT with author, thread_id,
>   comment_id, parent_id, score, sort, depth. Verified live: 38 records, **26 distinct authors**,
>   3 threads, top/new/controversial rotation. Reddit's own search is retired (it returned anime/AITAH
>   for niche B2B queries). Corroboration is now counted by DISTINCT AUTHOR — ten comments in one
>   thread are one report, not ten.
> - **Relevance-first evidence selection.** Synthesis ordered by `grade`, which is retrieval fidelity
>   not usefulness — github_api (grade A) once buried every web result. Extraction now emits a
>   constrained `answers_question` / `facet` decision and synthesis orders on it.
> - **No more fixed sleeps** waiting on reach/reviewers — both orphaned real results for weeks
>   (lessons #17).
>
> **2026-07-24 — CROSS-RUN SYNTHESIZER added (two-model synth+critic).** Gave Claude/Opus a
> SECOND role beyond the gate: an on-demand consolidator that reads many delivered runs together and
> produces one intelligence brief (this was the long-deferred "staged/chained runs" item). Chain runs
> in the reviewer container ($0, subscription): Claude Opus drafts → Codex `gpt-5.6-terra`
> adversarially critiques → Claude revises, + a transparency "Adversarial review (Codex)" appendix.
> Trigger: `synthesize-project` skill → `/api/synthesize` (run_ids) → poll `/api/synthesis/{id}`;
> discovery via `/api/runs`. Tables: `cross_syntheses` (migration_005). Proven live (synthesis 1,
> runs 14/15/18): Codex caught the draft falsely claiming two runs agreed when they contradicted,
> forcing a more careful final brief — the two-model architecture measurably beat a single pass, $0.
> Owner decision: Claude STAYS at the per-finding gate (telemetry showed it's not redundant with
> Codex — different check); this ADDED a role, didn't move it.
>
> **2026-07-24, earlier — WEB SEARCH added (the missing spine).** The engine could only READ a
> given URL or search inside one platform — no open-web search. Added a self-hosted **SearXNG**
> container (`deploy/searxng-run.sh`, 127.0.0.1:8888, $0, no key) + a `web_search` legit collector:
> SearXNG finds relevant URLs by keyword → existing Jina reader pulls each page → evidence.
> `web_search` is now the lead ALWAYS_SOURCE. Proven live (run 14): the 3PLGuys question that
> previously returned anime/K-pop garbage now returns 3plguys.com/peptides, prepcenter.com, 3plhub,
> real Trustpilot reviews, and a report with marketing-vs-review ⚔ contradictions firing correctly,
> honest gaps, $0.0033. Reddit `.json` was attempted but is hard-blocked (lessons.md #14) — reverted
> to HTML scrape; web_search is the real Reddit-relevance path now anyway.
>
> **2026-07-24 — outage + fix: no supervisor for bare host processes.** `cloudflared`
> self-updated and exited; `@reboot`-only cron never brought it back, so chat.example.com was down
> for hours before anyone noticed. Fixed: `deploy/watchdog.sh` (cron, every minute, pgrep + restart
> for web app / caddy / cloudflared — docker containers already self-restart) +
> `cloudflared --no-autoupdate`. See lessons.md #12/#13 for the two bugs hit while fixing this
> (flag-order syntax, then a pgrep pattern that stopped matching after the flag was added).

## What this project is
A read-only research engine on a 4GB VPS (Hetzner CX23, Helsinki), pivoted from a larger
autonomous-system master plan — owner chose to build the research core first, other layers
deferred indefinitely. (Addresses/domains in this file are placeholders in the public repo.)

## Where things actually are right now

**Mission Control** — one console, one login, five panels (Overview/Research/Chat/Services/Cost) —
live at **research.example.com**, gated by **Cloudflare Access** (email OTP to the owner's real
email; app-level password was removed once Access proved it gates correctly — Access is now the sole
auth layer). Dark OKLCH theme, cyan accent, built with the Hallmark design skill.

**Research pipeline** — submit a question → collectors (legit + walled) → OpenRouter synthesis →
deterministic release gate → optional Codex/Claude adversarial review → cited report. Proven live on
real GitHub data (run 3: 25 real issues → 13 well-cited findings, $0.0128) and on live Reddit +
Instagram reads through the walled path.

**Model stack, LOCKED (owner directive, permanent, pinned exact slugs not aliases):**
- Hermes chat/director brain: `tencent/hy3-20260706` (agentic-search leader, anti-hallucination design)
- Synthesis: `minimax/minimax-m3-20260531` (1M context)
- Bulk/per-platform extraction: `nvidia/nemotron-3-ultra-550b-a55b:free`
- Reviewers: Codex `gpt-5.6-terra` (not the `gpt-5.6` alias — that routes to Sol) + Claude
  `claude-opus-5` (upgraded from `claude-opus-4-8` on 2026-07-24 when Opus 5 released; still an exact
  slug, NOT the `opus` alias — that drifts to newest). Both ride the owner's existing
  subscriptions, zero incremental cost.

**Hermes chat is live** — real conversational agent at the Chat panel, Hy3-powered, can trigger the
research pipeline itself via the `run-research` skill (calls the web app's `/api/ask` + `/api/run/{id}`
JSON endpoints). Getting the websocket working through the tunnel took real debugging — see lessons.md
#10; the fix was a small Caddy loopback shim, not a Hermes/cloudflared config flag.

**Reviewer layer is live** — isolated container, Codex + Claude CLIs, both authenticated (Codex via
device-auth, Claude via a laptop-generated `setup-token`), models locked as above, verified end-to-end
(a synthetic Codex 'reject' correctly blocked delivery via the release gate).

**Walled sources — major architecture pivot mid-session.** agent-reach's OpenCLI/rdt-cli backends were
a dead end: both require a *live desktop browser session* to reuse, which a headless VPS fundamentally
cannot provide (confirmed via their own docs, not assumption). Replaced with **Camoufox** (stealth
Playwright-Firefox fork, ~200MB, true headless, C++-level fingerprint spoofing) — the reach container
was rebuilt around it.

- **Reddit: working, no account needed.** Reddit renders to logged-out visitors; the only actual
  blocker was the VPS's datacenter IP (confirmed via Reddit's own "blocked by network security, log in
  or use your developer token" message). Fixed with a residential proxy (DataImpulse, $5/5GB
  pay-as-you-go, country-pinned to avoid random-exit-location abuse flags).
- **Instagram: working, burner logged in.** `burner@example.com` (Gmail refused — the phone
  number had hit its new-account verification cap; Outlook had no such wall). Login done via a
  browser-based flow: Camoufox headful on a virtual display, streamed over noVNC through an SSH tunnel,
  owner logs in through the *same residential proxy* the headless reads use (so the session's IP is
  consistent, not laptop-then-VPS). Session persisted to `state/instagram.json`, real posts verified
  live (captions, hashtags, real content).
- **Facebook: blocked.** Same burner got instantly suspended by Facebook on account creation — no
  identifiable trigger, appeal submitted by owner, not pursuing further right now.
- **X: working, via the official paid API.** Burner signup got blocked same day as the Gmail/FB walls
  (lessons.md #7), so owner switched to console.x.com's pay-as-you-go API instead — real minimum credit
  purchase confirmed live at **$5** (not the $10+ estimate floated earlier). Bearer token generated,
  wired into `.env` as `X_BEARER_TOKEN`, `collectors.legit.collect_x` verified live (run 7, 10 real
  posts stored, real content confirmed — e.g. actual tweet text about agentic model tool-calling).
  No burner, no proxy, no suspension risk for this one.

## Open decision, not yet resolved
Burner credentials saved at `/home/trader/hermes-build/.burner-creds.env` (chmod 600, server-only)
currently hold the burner's **email** login only. The actual Instagram account password was used once
during interactive login and was never captured to that file — reach re-authenticates via the saved
browser session (`state/instagram.json`), not the password. That's fine *while* the session stays
valid, but if it ever fully expires, nothing can re-login without the real IG password — which
partially undercuts the owner's original ask ("save creds in case Hermes needs to login again on its
own"). Not yet decided whether to go capture it retroactively. See todo.md.

## Real budget/verification gaps (called out, not yet closed)
- `docker stats` under full concurrent load (web app + Hermes + reviewer + reach/Camoufox) has never
  actually been measured against the plan's <3.5GB target — more services are running now than when
  that budget was set.
- Kill-switch has never been drilled end-to-end.
- Injection-resistance was proven once early on synthetic DB-inserted data; not re-verified since the
  reviewer layer and Camoufox sources were added.
- No single research run has chained a walled source through the *entire* pipeline (collect via
  reach → synthesize → review → gate → report) — each stage proven individually, never all together
  with real walled evidence in the mix.
