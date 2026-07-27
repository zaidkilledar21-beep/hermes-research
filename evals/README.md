# Discovery eval: how we prove a retrieval change helped

Before this existed, every retrieval change was argued from one run's anecdote. That cannot tell a
real improvement from a lucky question, and the engine has already shipped changes whose value was
never actually demonstrated.

This eval never starts a research run. It creates no `research_runs` row, reads no pages, calls no
paid model, and writes nothing to the database. It does not even need a `DATABASE_URL`. It stops
precisely where the money starts: it builds the query plan, asks SearXNG what those queries surface,
applies the production selection policy from `pipeline/select.py` (the same constants, imported
rather than copied), and scores what *would* have been read.

## Run it

```bash
python -m evals.run_eval --plan-only
```

Offline, with no SearXNG, no database, and no network. This scores the query plan only: does the plan
anchor failure language on the vendor the question names? Run this version on a laptop or in CI.

```bash
python -m evals.run_eval --out evals/report.json
```

Full mode. It needs SearXNG reachable (via `SEARXNG_URL`, or `127.0.0.1:8888` on the VPS) and nothing
else, because discovery lives in `collectors/search.py` specifically so this harness never has to
import the evidence store to ask a search engine a question. It probes the configured endpoint first
and falls back to `--plan-only` rather than burning one HTTP timeout per query.

```bash
python -m evals.run_eval --plan-only --legacy-plan     # what the pre-v2 engine planned
python -m evals.run_eval --save-baseline evals/baseline.json
python -m evals.run_eval --baseline evals/baseline.json
```

The delta output is the point. A mean that moved 0.02 is noise, while a named question that dropped
0.3 is a regression with an address. `evals/baseline-legacy-plan.json` ships as a reference point:
the pre-failure-family plan scores a mean aim of 0.85, the current plan scores 1.00, and four named
questions move between them.

## What is scored

| metric | meaning |
| --- | --- |
| `aim` | did the plan put the expected complaint term in the same query as the vendor alias |
| `signal` | complaint language in the URL's own slug, never the vendor name, which is not a complaint |
| `reach` | share of selected URLs on venues where the answer plausibly lives |
| `diversity` | distinct venues divided by URLs selected, which guards the fix's own failure mode |
| `vendor_pull` | share of the budget spent on vendor and affiliate surfaces (a penalty, weighted negative) |
| `base_share` | share of reads still held by the base topic query (the control questions' guarantee) |

This scorer follows two rules, both of which its first draft broke.

The first: the oracle never reads its answers from the generator. `expect_terms` are literal strings
in `questions.json` rather than references to `queries.FAILURE_FAMILIES`. Replace the failure
vocabulary with nonsense and the score falls, and there is a test asserting exactly that. An oracle
that imports its answer key from the code under test always reports success.

The second: a venue gets no credit for existing. Reddit is scored on the subreddit rather than on
`reddit.com`, and `signal` separately asks whether the URL's slug is about a failure at all. Scoring
hosts alone gave run 28's exact failure, 166 items of generic r/logistics chatter, a perfect 1.0.

These metrics are proxies, deliberately. There is no ground-truth document set to compute recall
against, because the population of relevant threads is unknowable. Compare scores between engine
versions on these fixed questions. An absolute score on its own means nothing.

## The questions

`questions.json` holds 10 hand-labelled questions. Seven have the shape the engine keeps failing on,
a specific operator complaint about a specific vendor. The other three are controls (brandless,
regulatory, technical) and exist to catch the opposite failure, where complaint expansion drowns out
a question that was never about failure.

Note what protects the controls. Complaint expansion is unconditional: every pooled question gets
failure queries, including "RUO reserve" for the regulatory one. That works because the base topic
query holds a reserved floor of the read budget (`select.BASE_TIER_FLOOR`), so the expansion can add
candidates without displacing `fda.gov`. `base_share` is the metric that would catch it if that floor
ever broke.

## Deliberate exclusion: the registry

The vertical source registry (`pipeline/registry.py`) is off by default and needs `--use-registry` to
opt in. That is a divergence from production, where it is always on. The registry is mutable state
that grows with every run, so including it would mean two eval runs of identical code could score
differently, and a benchmark that cannot be replayed is not a benchmark. Measure the registry's
contribution as its own labelled comparison instead of letting it drift through every other number.
