# Contributing

Fork it, adapt it, point it at your own vertical. If you want to send something back, the rules
below are the ones the repo already holds itself to.

## The one rule that matters

**No prompt or retrieval change merges without beating the frozen baseline.** Every conclusion in
this repo is supposed to come from a number, because the alternative is arguing from "the run looked
better", which is how it shipped changes whose value was never demonstrated.

```bash
python -m evals.run_eval --plan-only                       # query planning, offline and free
python -m evals.capability_metrics --runs 29-42 \
    --baseline evals/baseline-capability.json              # scorecard vs the frozen numbers
```

If your change moves a metric down, either fix it or say plainly in the PR that you are trading that
metric for something else and why. A regression named in the PR is fine. A regression discovered
later is the thing this rule exists to prevent.

## Running the tests

```bash
python -m unittest discover tests    # 238 tests, pure and mocked, no network or database
```

Tests never touch a database, a search engine, or a browser. `psycopg` is stubbed at import time and
collaborators are replaced with recorders, so what is under test is the orchestration logic rather
than the infrastructure. Keep new tests in that style: if a test needs a live service, it is testing
the wrong thing.

## What good changes look like

**Fail soft, and leave a signal.** Every optional stage degrades to the deterministic path when it
fails. That is only safe if the failure is visible afterwards, so each one records state somewhere a
human will see it: `sub_questions.plan_state`, a rollup in `research_runs.notes`, a row in
`agent_runs`. A feature that fails silently can be completely dead and still look healthy.

**Enumerate the consumers.** If you add a `disposition` value or change what counts as an accepted
finding, find every reader of that data before you finish: the release gate, `report.py`, the
cross-synthesis packet, the registry, the scorecard. A gate that only holds at one layer is not a
gate.

**Feature flags default off.** New stages ship disabled (`PLANNER_ENABLED`, `MAX_REVISION_ROUNDS`,
`FOLLOWUP_ENABLED`) so a maintainer can verify them against their own runs before trusting them.

**Pin model slugs exactly.** Never an alias. `tencent/hy3-20260706`, not `hy3`. Aliases drift to
whatever is newest and silently change behaviour under you.

**Never point a precision call at a free tier.** Free models are metered three separate ways (per
minute, per day, provider concurrency) and the daily cap cannot be waited out inside a working
session. Bulk cleanup on a free slug is fine; a relevance verdict or a query plan is not.

## Before you open a PR

Read [`docs/lessons.md`](docs/lessons.md). It is 30 numbered failures with their root causes, and
most of them are mistakes that looked reasonable at the time. It will save you from repeating at
least one of them.

## Security

The two-container isolation is load bearing, not decorative. The walled-source scraper holds no real
credentials and cannot read an engine secret; the reviewer container has no database access. Scraped
text is tagged `UNTRUSTED_EVIDENCE` and is data, never instructions. If a change would weaken any of
that, it needs a much better reason than convenience.

Never commit a real key. Config templates reference `${ENV_VAR}` only. If you think a secret reached
a remote, assume it is burned: rotate first, clean history second (see lesson 18).
