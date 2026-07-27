# Build history

The public repo starts from a squashed root (the private lineage carried operational details —
server addresses, account identifiers — that don't belong in public blobs). This file preserves
the engineering narrative those commits told. Fuller running commentary lives in
[docs/internal/status.md](docs/internal/status.md); the distilled mistakes live in
[docs/lessons.md](docs/lessons.md).

## v1 — the spine (days 1–2)

- **Foundation:** Hermes agent gateway (Hy3 director) on a 4GB VPS, Neon Postgres, FastAPI
  Mission Control console behind Cloudflare Access, Telegram delivery. Two-container security
  floor from day one: the stealth-browser scraper holds zero real credentials; the reviewer
  container holds zero database access.
- **Pipeline:** submit → decompose → collect (X API, GitHub, HN, RSS, web reader) → synthesize
  (findings labelled observed / inferred / unknown, evidence ids mandatory) → deterministic
  release gate → cited report. First real run: 25 GitHub issues → 13 cited findings, $0.0128.
- **Walled sources pivot:** the original scraper backends needed a live desktop browser session —
  impossible headless. Replaced with Camoufox (stealth Playwright-Firefox). Reddit works
  logged-out behind a residential proxy; Instagram via a burner session; X via the official
  pay-as-you-go API after burner signups burned.
- **Open-web search:** self-hosted SearXNG + reader — the engine's missing spine. The question
  that previously retrieved K-pop garbage started returning real vendor pages, real reviews,
  real contradictions.

## v2 — aim over volume (days 3–5)

- **Credibility tiering:** claim-class (primary_authority / reference / independent_review /
  vendor_marketing / community) orthogonal to retrieval grade. Community anecdote became a
  first-class, honestly-labelled signal (`community_signal`) instead of noise.
- **Failure semantics:** typed synthesis states (a parse failure can never report as "no
  findings" — one run had discarded 5,197 chars of completed analysis that way); per-finding
  dispositions (one malformed finding stopped nuking whole reports); fabricated citations made
  detectable instead of laundered.
- **Comment-level Reddit:** one record per post/comment with author, thread, score, depth.
  Corroboration counted by distinct author — ten replies in one thread are one report.
- **Discovery targeting:** run 28 proved volume was solved and aim was not (166 retrieved, 156
  correctly rejected). Failure-language query families, tiered diversity selection with reserved
  floors, a vertical source registry (the engine's first cross-run memory), and a judged offline
  eval so retrieval changes argue from a number. Legacy plan aim 0.85 → 1.00.
- **Reliability:** search throttling detected and reported (a throttled search is
  indistinguishable from an empty one unless you look at the engines); run-wide budget ceilings;
  free-tier metering mapped (per-minute, per-day, provider concurrency) with a paid fallback.

## v3 — closing the gap to expert-level (days 6–7)

Driven by real feedback: a 14-run diligence campaign consolidated into a brief the client called
"extremely generic". The diagnosis was measured, not vibed — a new SQL scorecard showed the brief
kept 98 of the 401 named entities its own findings contained (retention 0.244), retrieval aim
averaged 0.204, and every run had operated on a single sub-question.

- **Capability scorecard first**, baseline frozen before any change. Keep-or-revert on numbers.
- **LLM query planner** (Hy3): anchors, domain vocabulary, intent-tagged queries, expected-
  evidence descriptions threaded into extraction's relevance verdict. Shipped as a strict
  superset of the deterministic floor after the eval measured that replacing it cost aim
  (1.00 → 0.875). The same eval had already caught the planner's think-token truncation —
  two real defects found for half a cent, before any live run.
- **Brief de-genericized:** named-specifics sections, a hard no-category rule, and a
  `lost_specifics` adversarial-critic dimension. Retention 0.244 → **0.838** at $0.
- **Revision loop:** rejected findings get one batched defend-or-revise pass; a defence requires
  a verbatim quote that actually appears in the cited evidence.
- **Gap-driven iteration:** unknowns and unresolved contradictions spawn a bounded second
  collection round on a halved budget, convergence-gated.
- **Quote-anchored citations + figure cross-checks + screening ledger:** fabrication detection at
  content level; conflicting numbers forced to meet; PRISMA-style flow accounting in every report.
- **Vertical memory (priors):** accepted figures accumulate per vertical; large deviations from
  the stored median get flagged as reviewable findings. Never auto-rejects; cold-start honest.
- **Primary sources:** SEC EDGAR, CourtListener, openFDA enforcement — free, keyless, grade A.
- **Model bakeoffs:** both measured, both said keep. ~$0.12 total.

238 tests, all pure/mocked. Total model spend across the entire v3 upgrade including every test
and bakeoff: well under one dollar.
