# Lessons: patterns to stop repeating

Read this before touching the reach/reviewer/hermes containers or the deploy scripts again.

## 1. PowerShell → SSH compound commands break silently
Embedded double quotes, pipes, or Unix `$VAR` syntax inside a single `ssh host "big; compound; command"` string frequently get
mangled by PowerShell's *local* parser before it ever reaches the remote shell. Symptoms:
"Unterminated quoted string", "term not recognized", or a later unrelated part of the command
failing while an earlier part silently never ran. Hit this ~8 times this session.
**Fix:** one action per `ssh` call when the command has quotes/pipes/special chars. Don't chain many
steps with `&&` inside one quoted block just to save a round-trip. Split it.

## 2. Docker named volumes only inherit image content on FIRST mount
A named volume mounted over a directory copies the image's content into it once, when the volume is
empty. Rebuilding the image later does **not** update an already-populated volume. The stale content
wins. Caused a full crash-loop (`reach_camoufox.py: No such file`) when a volume from an old image shadowed the new one.
**Fix:** mount volumes at the narrowest path that actually needs to persist (e.g. `state/`, not the
whole home dir) so image rebuilds (new code, deps) actually take effect on `docker run`.

## 3. Fresh named volume ownership must be pre-set in the image
For a non-root container user to write into a fresh named volume, the Dockerfile must `mkdir` +
`chown` that exact path as root **before** `USER <name>`. Otherwise the volume mounts root-owned and the
app gets `Permission denied` (hit this for both reviewer's `.codex` dir and reach's state dir).

## 4. Camoufox/Playwright: `storage_state` is a context param, not a launch param
`Camoufox(storage_state=...)` throws `BrowserType.launch() got unexpected keyword argument`. `storage_state` belongs on `browser.new_context(storage_state=...)`. `proxy` and `geoip` ARE launch-level and
belong in `Camoufox(...)`. Split: `_browser()` (launch-level, proxy/geoip) → `_page(browser,
storage_state=...)` (context-level).

## 5. `set -euo pipefail` kills the whole script on one soft failure
A single non-critical step (e.g. `chmod` on a file some other uid owns) aborts everything after it,
silently. Wrap best-effort steps (`chmod -R 777 $DIR`, cleanup, etc.) with `|| true` / a fallback instead of
letting them can the launcher.

## 6. Residential proxy: pin the country, don't leave it random
A proxy on default/random targeting returns a **different exit country every session**. That
inconsistency (not any single country) is the bigger abuse-detection flag. A burner that logs in
from Brazil once and the US next time reads as account takeover. Pin the proxy country and keep it
matched to whatever country the saved login session was originally created under (`REACH_PROXY_COUNTRY`, appended
to the DataImpulse username as `__cr.<code>`).

## 7. Rapid-fire burner signups across platforms compound
Creating several new accounts (Gmail → Facebook → X) back-to-back in one browser session, same day,
tripped independent anti-fraud walls on **all three** platforms within an hour (Gmail phone-verify
cap, FB instant-suspend, X `EmailSignupWebBlocked`). Likely each platform's own fraud model flags "burst of new
accounts, same session" independently. **Space burner creation out across sessions/days**, don't
chain them.

## 8. Code that assumes it's inside a container breaks when the host also calls it
`reach_bridge.py` and `reviewers.py` hardcoded `/app/dropbox` and `/app/review`. Fine inside a container, broken when the
*host* pipeline (`pipeline/run.py`) imports the same module directly.
**Fix:** env-overridable paths (`DROPBOX_DIR`, default falls back sanely) instead of hardcoded
container-only paths, for anything shared between host and container execution.

## 9. Hermes OpenRouter provider: use `hermes config set`, not hand-edited YAML
Hand-writing `provider: custom / base_url / api_key` into `config.yaml` looked right but the agent still reported "No LLM provider
configured". Hermes has OpenRouter as a genuine first-class provider.
**Fix:** `hermes config set model.provider openrouter` + `model.default <slug>`, `OPENROUTER_API_KEY`
in the environment, then restart. Don't hand-edit the custom-endpoint block for OpenRouter
specifically.

## 10. Hermes dashboard behind a tunnel needs BOTH a Host fix and an Origin fix
It has a DNS-rebinding guard (rejects non-loopback `Host`) **and** a separate websocket Origin
check. `HERMES_DASHBOARD_PUBLIC_URL` alone doesn't satisfy the Host guard. cloudflared's `httpHostHeader` rewrite alone doesn't
satisfy the Origin check either. **Fix that actually worked:** a tiny Caddy loopback shim between
the tunnel and Hermes, rewriting both `Host` and `Origin` to `127.0.0.1:9119`.

## 11. Don't guess CLI flags/config formats for infra you're about to deploy
Every time a WebSearch verification step was skipped or shortcut, it cost a broken deploy (Camoufox
storage_state, Hermes provider config, the reach `agent-reach reddit` subcommand that turned out not to exist).
When wiring a new CLI/library into production infra, verify the actual flag/API shape first. Cheaper
than a live debug cycle after.

## 12. Bare host processes need a supervisor, not just `@reboot`
`cloudflared` silently self-updates and exits (2026-07-24: killed the tunnel to chat/research.example.com
for hours, nobody was watching). `@reboot`-only cron starts a process once at boot and never again. Any crash, self-update, or OOM kill after that is permanent until someone notices and SSHes in.
**Fix:** `deploy/watchdog.sh` + `* * * * * watchdog.sh` cron entry. Checks each bare process (web app,
caddy, cloudflared) via `pgrep` and restarts only if dead, every minute. Docker containers (hermes,
reach, reviewer) don't need this: `docker run --restart unless-stopped` already covers them. Also added `cloudflared --no-autoupdate` (global flag,
MUST come before `tunnel run`, not after: `cloudflared tunnel run research --no-autoupdate` fails with "accepts only one argument") to kill the
root cause, not just the symptom.

## 18. A hand-rolled secret regex that finds nothing is NOT proof there are no secrets
Before the first push I scanned with `sk-[a-zA-Z0-9]{16,}` and got zero hits, and treated that as clearance. But
**OpenRouter keys are `sk-or-v1-<hex>`**. The hyphens in `or-v1-` break that character class immediately, so
the pattern could never match the very key that was sitting in `deploy/hermes-config.yaml`. GitGuardian caught it after
the push; the key had to be revoked.
**Rules:**
- Never trust a self-written pattern as a clean bill of health. Match against REAL provider formats
(`sk-or-v1-`, `sk-ant-`, `ghp_`, `xoxb-`, `AKIA`, …) and allow `[A-Za-z0-9_-]` throughout.
- Independently EYEBALL every config/deploy file that could plausibly hold a credential
(`deploy/*.yaml`, `*config*`) before a first push. Greps miss what you did not anticipate.
- Assume any secret that reached a remote is BURNED. Rotate first, purge history second; a history
rewrite does not un-expose it.
- Config templates in git must reference `${ENV_VAR}`, never a literal value.

## 17. A fixed sleep waiting on an async subsystem silently ORPHANS results
Hit twice, both losing real data for a long time before anyone noticed:
- `reviewers.run_reviews` slept a fixed 90s. Reviews are 2 sequential CLI calls per finding, so any
  run with >~4 findings had verdicts arrive after the ingest gave up. Orphaned verdict files were
  found for runs 6, 19, 21, 22, 24, 25, 26. Several "delivered" reports were only PARTIALLY reviewed
  and nothing surfaced that.
- `run.py`'s reach ingest slept a fixed window. Reading browser-rendered threads takes minutes; run
27's entire community-evidence batch was written after the caller stopped polling and was lost.
**Fix pattern:** track the unit of work (request id / packet id) and poll until each reports in, with
a generous ceiling. Exit as soon as everything is accounted for. Never `sleep(N); then_read()`. Also: log what was
ingested. Both bugs were invisible because success and silence looked identical.

## 16. Adding a source means registering it in EVERY place, or it fails silently
`reddit_threads` had to be added to: the `sources` table, `reach_bridge.VALID`, `READERS` in the reach container, `common.WALLED_SOURCES`
(**security**: missing here means scraped text is tagged TRUSTED_EVIDENCE and skips injection
quarantine), `run.py`'s `WALLED` (missing = ingest never fires), `queries.SEARCH_TYPE` (missing = it gets the raw
300-char question instead of compressed keywords. Measurably worse: 3 threads discovered vs 8), plus
`SOURCES`/`ALWAYS_SOURCES` in the web app. Each omission failed QUIETLY and differently. When adding a
source, grep for an existing source id and match every hit.

## 15. Test the function the CALLER actually calls, not the one that looks right
`READERS["reddit_threads"]` maps to a thin request-unpacking wrapper, not `read_reddit_threads`. Calling the inner function directly
with the poller's dict produced `KeyError: slice(None, 25, None)` and sent me hunting a non-existent bug for several minutes.
The real code path was fine. Reproduce through the same entry point production uses.

## 14. Reddit's .json API is hard-blocked from the datacenter/proxy IP; HTML renders fine
Tried swapping the Reddit reader from HTML scrape to the `.json` endpoint (better relevance). Both
`www.reddit.com/search.json` AND `old.reddit.com/search.json` return "You've been blocked by network security" (143-byte block page) even
through the residential proxy. Reddit renders HTML to "browsers" but refuses the JSON API. Reverted
to HTML scraping (old.reddit.com/search, not blocked).
**Takeaway:** for Reddit, the high-relevance path is web_search (SearXNG → Google/Bing index Reddit
far better than Reddit's own search), NOT scraping Reddit search directly. Jina reading a specific
Reddit *thread* URL also gets 403'd. Reddit blocks server-side readers broadly.

## 13. A watchdog's process-match pattern must survive its own restart command changing
Changed the cloudflared start command (added `--no-autoupdate` before `tunnel run`) but left the watchdog's
`pgrep -f "cloudflared tunnel run"` pattern unchanged. The new command line (`cloudflared --no-autoupdate tunnel run research`) no longer contains that literal
substring, so the watchdog decided the healthy process was "down" and restarted it every single
minute, spawning 3 duplicate live tunnel connections before it was caught. **Fix:** match on the
stable trailing argument (`"tunnel run research"`) instead of a prefix that a flag can get inserted into. When you
change how a supervised process is launched, immediately re-check whatever pattern is used to detect
it's alive.

## 19. An oracle that reads its answer key from the code under test always passes
The first version of the discovery eval scored the query plan by importing `queries.FAILURE_FAMILIES`. The exact table
the generator emits from. Every question scored a perfect 1.0, and would have kept scoring 1.0 if
the failure vocabulary had been replaced with gibberish, because generator and scorer moved
together. Same failure on the discovery side: `reach` credited any URL on `reddit.com`, so run 28's 166
items of generic r/logistics chatter, the exact haul the relevance filter threw away, would have
scored a perfect run.
**Fix:** expectations are hand-written literals in `evals/questions.json`; Reddit is scored on the
SUBREDDIT, never the host; a separate `signal` metric reads complaint language out of the URL slug
(and deliberately does NOT count the vendor's name: `/vendor_launches_new_service/` is not a complaint). There is a test
that mocks the families to nonsense and asserts the score FALLS.
**Takeaway:** before trusting a metric, ask what would have to break for it to go down. If nothing
would, it is a mirror, not a measurement.

## 20. Sharper aim multiplies COST unless pooling is fixed in the same change
Failure-language families took queries per sub-question from 1-2 to 5. Left naive, that is 5x the
discovery calls, 5x the pages read, 5x the 30-60s browser renders, on a 4GB box. Three separate bugs
lived in that gap, all found by adversarial review, none by tests:
- Concatenating the query pools put every base-query hit ahead of every failure-query hit, so with a
  read cap of 8 the four extra searches could cost time and change the read set NOT AT ALL.
  (`select.interleave`. Take each query's best hit first.)
- Every sub-question got a fresh full allowance, so run cost scaled with however many facets the
director invented. (`run._Budget`. Run-wide ceilings, refunded when a facet finds nothing.)
- Reserving budget before selection charged facets for reads that never happened.
**Takeaway:** when a change increases the NUMBER of queries, the same commit must decide how many
RESULTS get read. Aim and budget are one change, not two.

## 21. Persistent state that steers retrieval needs an explicit revocation path
The vertical source registry is the engine's first cross-run memory, and its obvious failure mode is
a ratchet: a venue gets promoted, promotion buys it more exposure, the exposure produces the
evidence that justifies the promotion. Three rounds of review kept finding new ways in. Counting
usefulness per ITEM (two replies in one thread promote a venue), re-running a stage inflating the
counters, a vendor-run subreddit qualifying because its `credibility_tier` is `community` like any other, and
an old row staying eligible forever because the rate floor only bites on the row that keeps being
written.
**Fix, in layers:** promote by DISTINCT RUN; per-run ledger so a retry is not evidence; block credit
by BOTH tier and `page_ownership`; and make promotion revocable. A hit-rate floor plus a 120-day age-out,
so a venue that stops paying for its exposure drops out on its own.
**Takeaway:** for any self-written state, write down what REMOVES an entry before writing what adds
one. If nothing removes it, it is not a cache, it is a ratchet.

## 22. A throttled search is indistinguishable from an empty one: unless you look at the engines
Deploying the failure-language families took discovery from 1-2 queries per sub-question to ~5, and
the first live eval run had one question return ZERO candidates from five queries. It was not a bad
query: SearXNG's upstream engines had suspended us: `google cse: Our systems have detected unusual
traffic from your network`, `duckduckgo HTTP 403 (suspended_time=180)`. SearXNG answers 200
with an empty `results` list in that case, exactly as it does for a topic nobody has written about.
**Fix:** read `unresponsive_engines` from the SearXNG JSON. Empty results PLUS suspended engines
means throttled, not absent. One backoff-and-retry, a process-wide 1.5s pacing gap between queries,
a counter, and a note written to `research_runs.notes` so a throttled run can never be presented as a run that
found nothing.
**Takeaway:** this is the SAME bug class as the synthesis parse failure that reported itself as "no
findings" (status.md, run 20). Whenever a subsystem can fail in a way that LOOKS like a legitimate
empty answer, the empty answer has to carry its reason. Also: when a change multiplies request
volume against a third party, the rate limit is not an edge case, it is the next bug.

## 23. `sed` and `set -a` are both secret leaks in a deploy script
Wrote a launcher that injected a residential-proxy URL (which embeds its password) into a config
template. Codex found three separate leaks in about ten lines of shell:
- `sed "s|__PROXY_URL__|$PROXY_URL|"` puts the credential in the process's argv, and
`/proc/<pid>/cmdline` is world-readable, every local process could read it for the duration.
- `set -a; . ./.env; set +a` exports EVERYTHING in the env file (database URL, model keys, bearer
  tokens) into every subsequent command's environment, including `docker run`. `set +a` stops future
  auto-export; it does not un-export what was already sourced.
- A `sed` replacement treats `&` and `\` as syntax, so a password containing either silently
  rendered a corrupted URL, and a corrupted proxy URL fails as "authentication rejected", which
  reads like a credentials problem, not a quoting one.
**Fix:** a tiny Python renderer taking PATHS as arguments, reading only the specific keys it needs,
URL-quoting the credentials, and refusing to install a template with an unrendered placeholder.
**Takeaway:** if a deploy script handles a secret, the secret must travel by file or by stdin:
never as an argument, never through a blanket `source`.

## 24. Never destroy the working thing before proving the new thing serves
The same launcher did `docker rm -f searxng` and then `docker run -d`, and printed "searxng up". `docker run -d` only proves
a container was CREATED. A settings file SearXNG cannot parse would have left it restart-looping
with the previously-working container already deleted, and the script would have reported success.
During a research campaign that depends on search.
**Fix:** copy the live config aside first, health-check the actual JSON endpoint after start, and
roll back to the saved config if it does not serve within the window.
**Takeaway:** a deploy step that removes the current version owes you a proof-of-service and a way
back, not an echo.

## 25. The free model's rate limit is 20/min for the KEY, not per process
Bulk extraction ran 8 concurrent workers with no client-side limit. A 268-item run buried
OpenRouter's free-tier limit (`Rate limit exceeded: free-models-per-min`, X-RateLimit-Limit: 20) within seconds, and every subsequent
item came back as a 429 error body, which has no `choices` key, so the caller raised `KeyError: 'choices'` and
recorded a failed extraction. Fail-soft did its job and the run still delivered, on RAW un-cleaned
evidence with no `answers_question` verdicts, which silently disables relevance-first ordering AND starves
the vertical registry of its only signal. The symptom in the log (`KeyError: 'choices'`, 250+ times) reads like
a malformed model response, not like a rate limit.
**Fix:** client-side pacing at 18/min, shared ACROSS PROCESSES via a flock'd pace file
(`pipeline/pacing.py`) because the quota is per-key and two runs execute concurrently; explicit handling of
429/error bodies with a wait derived from the server's own `X-RateLimit-Reset`; workers cut 8 → 4 since
concurrency past the quota buys nothing.
**Takeaway:** parse the error body before indexing into the success shape, and when a limit belongs
to an account, the limiter has to live outside the process.

## 26. `options=-c statement_timeout=...` is rejected by Neon's pooled endpoint
Added a statement timeout to every registry connection (a Codex review point. A registry query must
never hang a run). Against Neon's POOLER it fails outright: `unsupported startup parameter in
options: statement_timeout`. Because the registry is deliberately fail-soft, this did not break any
run. It just meant the engine's brand-new cross-run memory recorded NOTHING on every run while
logging one line and continuing. A feature that fails soft can be completely dead and still look
healthy.
**Fix:** connect without the option, then `SET statement_timeout` as a statement (works pooled and
direct), and treat even that as best-effort.
**Takeaway:** fail-soft needs a positive signal somewhere. "No error" is not "it worked", after
deploying anything fail-soft, go and check that it actually produced a row.

## 27. A gate that only holds at one layer is not a gate
The release gate withholds findings that fail adversarial review, and `report.py` lists them as
WITHHELD rather than stating them. But `cross_synthesize._assemble_packet` selected findings with no `disposition` filter, so the
cross-run brief silently re-admitted every rejected claim as fact. Caught in the wild: a "2025-26
enforcement wave" claim naming a warehouse raid was rejected by both reviewers in run 31, correctly
withheld from that run's report, and then asserted, unmarked, in the consolidated brief over runs
29-36. I nearly carried it into a deliverable on the brief's authority.
**Fix:** the packet now selects `disposition = 'accepted'` only, and carries a `withheld_findings`
count per run so the brief can state what was excluded instead of quietly including it.
**Takeaway:** when you add a quality gate, enumerate every consumer of the gated data. A second
reader that queries the same table directly inherits none of the gate's judgement, and consolidation
layers are the likeliest place for that to happen, because they look like reporting, not retrieval.

## 28. The free tier has TWO limits, and the daily one is the one that bites
Paced bulk extraction at 18/min to stay under OpenRouter's documented 20 requests/minute, and it
worked, for a while. Fourteen research runs later, extraction started failing with a different
error: `Rate limit exceeded: free-models-per-day-high-balance`, `X-RateLimit-Limit: 1000`, `Remaining: 0`. There is a **daily cap of 1,000 free-model requests** on top
of the per-minute one. At ~120-300 items extracted per run, a multi-run campaign exhausts a day's
quota in a single afternoon, and every subsequent run silently degrades to raw, unscored evidence.
Also seen from the same provider: `Upstream error from Nvidia: ResourceExhausted: Worker local total
request limit reached (32/32)`. A provider-side CONCURRENCY ceiling that is neither of the two
documented rate limits.
**Fix:** cap items extracted per run (`EXTRACT_MAX_ITEMS`, default 120 = 2x the synthesis ceiling:
extracting 300 items to feed a 60-item synthesis was pure waste anyway), and treat "free" as three
separate budgets: per minute, per day, and provider concurrency.
**Takeaway:** when a quota is free, find out how many ways it is metered before designing around one
of them. And size batch work against the DAILY budget, because that is the one you cannot wait out.

## 29. A mangled diagnostic command can convict a healthy system
Hermes chat returned `HTTP 401: User not found.`, OpenRouter's exact wording for an unknown key, so the OpenRouter key
was rotated. Correct call, and the rotation worked. But the verification command run afterwards from
PowerShell came back `{"error":{"message":"No cookie auth credentials found","code":401}}`, which
reads like the brand-new key was ALSO dead.

It was not. PowerShell 5.1 stripped the inner double quotes while handing the string to `ssh.exe`,
so the remote shell saw `-H Authorization: Bearer sk-or-v1-...` as four separate words. curl took `-H Authorization:` as a header with an
empty value (legal), then treated `Bearer` and the key itself as two additional URLs. The tell was
in the output and easy to skim past: `-w` printed
**three** http codes, `401`, `000`, `000`, when one URL was requested, and `\n` came out as a
literal `n`. The 401 was OpenRouter answering a request that carried no `Authorization` header at
all. Note the message differs from the original failure: "User not found" = key present but unknown;
"No cookie auth credentials found" = no credentials sent. Two different 401s, two different causes.

**Fix:** avoid inner double quotes in PowerShell→ssh one-liners entirely. Either use a flag that
takes the secret as a single argument (`curl --oauth2-bearer $K`, no space to quote), or pipe a single-quoted
here-string into `ssh host 'bash -s'` and let the remote shell do all the parsing. Verify credentials with the
real consumer where possible: `python3 -m pipeline.model_check` proved all three slugs live in one call and has no quoting
surface at all.
**Takeaway:** when a check contradicts a change you just made, suspect the check before the change.
Read the shape of its output, not just the error string. A repeated `-w` field or a literal
`\n` means the command line was rewritten under you, and the error you are reading answers a
question you never asked. Related: #1 (the same mangling when it fails loudly instead of plausibly).

## 30. A reasoning model's think tokens spend from YOUR max_tokens, and an eval catches it for cents
The v3 query planner shipped with max_tokens=800. Plenty for the ~200-token JSON plan it asked for.
All ten eval questions came back `fallback_truncated`: Hy3 reasons before it answers, the reasoning spends from
the same pool, and 800 was gone before the first character of JSON emerged. synthesize.py already
knew this (its 12000 carries the comment "generous headroom past a reasoning model's think tokens").
The lesson existed in the codebase and was not consulted. Second catch, same session: with
truncation fixed, plan-only aim fell 1.00 → 0.875 because the planner REPLACED the deterministic
failure variants, and a model does not reliably re-derive the proven complaint phrasings ("missing
inventory", "do not use", "reserve"). Composition became a strict superset, deterministic queries
always survive, the model only ever adds, and aim returned to 1.00 with novelty up (0.315 → 0.438).
Both defects were found by `run_eval --planner` for ~$0.005 total, before any live run spent real collection
budget on a silently degraded plan. The fail-soft state (`fallback_truncated` recorded per sub-question) is
what made the first one visible at all. A plain fallback would have looked like the planner simply
not helping.
**Takeaway:** when calling a reasoning model, budget max_tokens for the thinking, not the answer:
and check whether an older module already learned the constant. When layering a model over a working
deterministic path, the model ADDS; it never replaces the part with guarantees. And build the cheap
offline eval BEFORE enabling the feature: it converts "the planner seems fine" into two concrete
defects at the cost of a rounding error.

## 30. A long-lived process cannot inherit config that did not exist when it started
`PLANNER_ENABLED=1` sat in `.env` and the runs still reported `planner: fallback_disabled`. The web
app had been up since two days before that flag was added, and `/api/ask` spawns `pipeline.run` with
`env=dict(os.environ)`, so every chat-triggered run inherited a uvicorn environment where the flag
did not exist. `/proc/<pid>/environ` confirmed it: every v3 flag `<ABSENT>` in a process whose config
file listed all of them.

What made it survive verification: CLI runs worked, because they were launched after `set -a; . ./.env`.
Both paths call the same `request_run()`, so "the chat path is proven because it shares the function"
felt safe. It is not the same claim. A shared function does not mean a shared environment, and the
difference only shows up in the caller nobody tested.

This is the same root cause as lesson 29's revoked key, where `docker restart` reused the captured
environment and kept authenticating with a rotated-away credential. Twice now, from the same
misconception: editing a config file does nothing to a process that already read it.

**Fix:** `pipeline/envfile.py` loads `.env` for variables that are ABSENT (never overriding an
explicit export, so per-run overrides still work), called at the top of `run.py`, `submit.py`, and
`web/app.py`, before the imports that read config at module scope, since anything imported first
freezes the parent's environment.
**Takeaway:** when a feature is gated by an env var, test it through the ENTRY POINT a user actually
touches, not the one that is convenient to script. And when a fix is "restart the thing," ask what
makes the next person's edit silently do nothing.

## 31. Think-token budgets scale with the INPUT's complexity, not the output's
Second time in one upgrade. `plan_queries` shipped with `max_tokens=800`, plenty for its ~200-token
JSON, and truncated all ten eval questions. Fixed to 4000. Then `decompose` shipped with 2000,
plenty for its ~300-token answer, and truncated on a question naming ten peptides plus import-alert
IDs plus exclusion clauses, falling back to a single seed sub-question.

The trap: budgeting from the ANSWER size is the obvious mental model and it is wrong. A reasoning
model spends think tokens working through the INPUT, so a long, multi-entity, constraint-heavy
question can exhaust the pool before emitting a single character of the short answer.
**Fix:** budget for the reasoning, not the reply, and scale the ceiling with the longest input the
call will realistically see (`decompose` gets 6000 where `plan_queries` gets 4000, because it reads
the raw question).
**Takeaway:** both times the fail-soft path worked perfectly and reported `truncated` honestly, which
is the only reason this was diagnosable at all rather than looking like a model that "just doesn't
decompose well."

## 32. Scrubbing a repo for publication turns config into documentation
Publishing meant replacing the real domain with `example.com` across the repo. Two days later a
routine sync of `web/app.py` to the box (for an unrelated fix) carried that placeholder onto
production, and the Chat panel rendered `chat.example.com's server IP address could not be found`.
The Hermes chat host was a hardcoded literal in an iframe `src`, so the scrubbed value was not a
comment, it was the live config.

The tell was there and I dismissed it. Diffing local against deployed before the sync, I saw the
domain line differ, read the two lines of context (both comments), and concluded "expected: the VPS
correctly runs the real domain while the repo has placeholders." The conclusion was right about WHY
they differed and wrong about what else differed the same way. A diff hunk's context is not its
extent.

**Fix:** the host reads from `HERMES_CHAT_URL`; unset, the panel says so rather than framing a dead
host. A public default is now inert instead of wrong.
**Rules:**
- Anything scrubbed for publication is a candidate for silently overwriting deployment config. After
  scrubbing, grep the placeholder across the tree and check each hit is genuinely prose, not a value.
- Environment-specific values belong in the environment. If a literal has to differ between the
  public repo and the deployment, that is the signal it was never a literal.
- When a pre-sync diff shows an expected difference, read the WHOLE file's hits for that token, not
  just the hunk that surfaced.

## 33. A diagnostic signal is not a diagnosis
On 2026-07-28 an operator agent watching the engine's stderr concluded "the research engine is in a
sustained upstream outage" and wrote a supervisor that re-fired runs up to six times each. The
engine was healthy. Runs were delivering the entire time — seven landed unattended during the
investigation, and the run it declared dead had ingested 453 items and was mid-extraction.

It read three signals, all of which the pipeline emits deliberately and honestly, as one fatal event:
- `engines unresponsive: brave, duckduckgo, startpage` — lesson #22's throttling. SearXNG still
  returned 80 candidates per query, and the run logged *"Coverage is INCOMPLETE, not absent."*
- `fallback_transport_failed` from decompose — lesson #26's fail-soft trace. The run CONTINUES.
- a free-tier daily 429 with an automatic switch to the paid model — lesson #28, verbatim.

Every one of those messages was accurate. The failure was that a label containing the word "failed"
described a path that had SUCCEEDED by design, and nothing in the system distinguished "degraded and
fine" from "dead" in a form a machine could read. The agent then re-asked a question that had already
delivered 18 findings, four times, because nothing refused a duplicate.

**Fixes:** `fallback_transport_failed` → `degraded_ok_transport_fallback` (genuinely fatal states,
like synthesize's `transport_failed`, keep fatal names). New `GET /api/run/<id>/health` returning
`progressing` / `degradations[]` / `fatal` so an agent never has to interpret prose.
**Takeaway:** if a fail-soft path's telemetry reads as an error, it will eventually be treated as
one. Naming is an interface. Prose written for a human tail-ing a log is not a machine contract, and
the moment an autonomous operator is watching, it needs one.

## 34. A read-then-act check is not a guard under concurrency
`budget_spent(run_id)` summed `WHERE run_id=%s` and eight call sites compared it against a variable
named `OPENROUTER_DAILY_CAP_USD`. So the "daily" cap was enforced per run: fourteen concurrent runs
meant a $28 ceiling, with every process correctly under its own budget. Invisible for months because
runs had always been serial — with one run at a time the two numbers are identical.

Worse, the pattern was `SELECT` → compare in Python → act. Under exactly the concurrency the cap
exists to control, every worker reads "under budget" and every worker proceeds. Splitting the
function into `spent_for_run()` and `spent_today()` fixes the VALUE and not the RACE.

Two further traps found while fixing it:
- Session advisory locks are not an option here. Neon's pooled endpoint is PgBouncer in transaction
  mode, so a session-level lock is not pinned to a backend across transactions. It tests green in a
  single session, which is what makes it dangerous. Same family as lesson #26.
- A single `INSERT … SELECT … WHERE (SELECT count(*) …) < n` is still not enough: under default
  READ COMMITTED two transactions can share a snapshot, both satisfy the predicate, and both insert.
  The gate runs at SERIALIZABLE with bounded retries.

**Takeaway:** for a guard, ask "what happens if two of these run at the same instant?" before asking
whether the number is right. And a guard whose own name is ambiguous (`budget_spent` — whose budget?)
will be used ambiguously; the fix was deleting the name, not aliasing it.

## 35. A non-atomic write into a polled directory is a race, and a silent `except` hides it
The reviewer dropbox is a shared directory: the host writes `req/<name>.json`, the container polls
every 5 seconds and writes `out/<name>.json` back. Both sides used plain `write_text()`. For the
per-finding packets this pattern has carried for months — they are ~1KB, so the window between
"file exists" and "file is complete" is too small to lose a coin flip in.

The cross-synthesis packet is 265KB. The reviewer caught it half-written, `json.loads` raised, and
this ran:

```python
except Exception:
    req_path.unlink(missing_ok=True)   # silently DELETES the request
    return
```

The request vanished, no result was ever written, no log line was emitted, and the host sat polling
an `out/` file that could never appear until its own timeout expired. The visible symptom — a
consolidation that "hung" — pointed at the model, the packet size, and the timeout, none of which
were the cause. Two separate hours went into the wrong suspects.

**Fixes:** every dropbox write on both sides now goes to a same-directory dotfile and `os.replace()`
onto the target — `rename(2)` is atomic within a filesystem, so a poller sees nothing or everything.
An unparseable request is renamed to `.unparseable` and logged loudly instead of deleted.
**Takeaways:**
- A file appearing in a watched directory is not a promise that it is finished. If a peer polls for
  existence, the producer owes it atomicity.
- Never `unlink()` in an `except` you cannot explain. The unreadable thing is the only evidence of
  why it was unreadable; destroying it converts a five-minute diagnosis into a blind search.
- Bugs whose probability scales with payload size lie dormant through every small test and fire the
  first time the system does something ambitious.
