# Discovery eval — how we prove a retrieval change helped

Before this existed, every retrieval change was argued from one run's anecdote. That cannot tell a
real improvement from a lucky question, and the engine has already shipped changes whose value was
never actually demonstrated.

**This eval never starts a research run.** No `research_runs` row, no page reads, no paid model, no
database writes — it does not even need a `DATABASE_URL`. It stops precisely where the money starts:
it builds the query plan, asks SearXNG what those queries surface, applies the production selection
policy (`pipeline/select.py` — the same constants, imported, not copied), and scores what *would*
have been read.

## Run it

```bash
python -m evals.run_eval --plan-only
```

Offline. No SearXNG, no database, no network — scores the query plan only (does the plan anchor
failure language *on* the vendor the question names?). This is the version to run on a laptop or in
CI.

```bash
python -m evals.run_eval --out evals/report.json
```

Full mode. Needs SearXNG reachable (`SEARXNG_URL`, or `127.0.0.1:8888` on the VPS) and nothing else
— discovery lives in `collectors/search.py` specifically so this harness never has to import the
evidence store to ask a search engine a question. Probes the configured endpoint first and falls
back to `--plan-only` rather than burning one HTTP timeout per query.

```bash
python -m evals.run_eval --plan-only --legacy-plan     # what the pre-v2 engine planned
python -m evals.run_eval --save-baseline evals/baseline.json
python -m evals.run_eval --baseline evals/baseline.json
```

The delta output is the point. A mean that moved 0.02 is noise; a named question that dropped 0.3 is
a regression with an address. `evals/baseline-legacy-plan.json` ships as a reference point: the
pre-failure-family plan scores **mean aim 0.85**, the current plan **1.00**, and four
named questions move.

## What is scored

| metric | meaning |
| --- | --- |
| `aim` | did the plan put the expected complaint term **in the same query as** the vendor alias |
| `signal` | complaint language in the URL's own slug — **never** the vendor name, which is not a complaint |
| `reach` | share of selected URLs on venues where the answer plausibly lives |
| `diversity` | distinct venues ÷ URLs selected — guards the fix's own failure mode |
| `vendor_pull` | share of the budget spent on vendor/affiliate surfaces (penalty, weighted negative) |
| `base_share` | share of reads still held by the base topic query (the control questions' guarantee) |

Two rules this scorer follows, both of which its first draft broke:

1. **The oracle never reads its answers from the generator.** `expect_terms` are literal strings in
   `questions.json`, not `queries.FAILURE_FAMILIES`. Replace the failure vocabulary with nonsense and
   the score falls — there is a test that asserts exactly this. An oracle that imports its answer key
   from the code under test always reports success.
2. **A venue gets no credit for existing.** Reddit is scored on the **subreddit**, never on
   `reddit.com`, and `signal` separately asks whether the URL's slug is about a failure at all.
   Scoring hosts alone gave run 28's exact failure — 166 items of generic r/logistics chatter — a
   perfect 1.0.

These are still proxies, deliberately. There is no ground-truth document set to compute recall
against; the population of relevant threads is unknowable. Compare scores between engine versions on
these fixed questions. An absolute score on its own means nothing.

## The questions

`questions.json` — 10 hand-labelled questions. Seven are the shape the engine keeps failing on (a
specific operator complaint about a specific vendor); three are controls (brandless, regulatory,
technical) that exist to catch the opposite failure, where complaint expansion drowns out a question
that was never about failure.

Note what protects the controls: complaint expansion is **unconditional** — every pooled question
gets failure queries, including "RUO reserve" for the regulatory one. That is fine because the base
topic query holds a reserved floor of the read budget (`select.BASE_TIER_FLOOR`), so the expansion
can add candidates but cannot displace `fda.gov`. `base_share` is the metric that would catch it if
that floor ever broke.

## Deliberate exclusion: the registry

The vertical source registry (`pipeline/registry.py`) is **off by default** (`--use-registry` opts
in), and yes — that is a divergence from production, where it is always on. It is mutable state that
grows with every run, so including it would mean two eval runs of identical code could score
differently. A benchmark that cannot be replayed is not a benchmark. Measure the registry's
contribution as its own labelled comparison instead of letting it drift through every other number.
