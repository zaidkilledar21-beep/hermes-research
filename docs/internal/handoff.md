# Handoff: what to actually do next

Narrative/context: `docs/internal/status.md`. Patterns/mistakes: `docs/lessons.md`. Checkable backlog: `docs/internal/todo.md`. This file =
"what's the very next action," kept short on purpose. Update it at the end of every session.

## Right now (as of 2026-07-25)

**Discovery targeting is DEPLOYED.** migration_009 applied to Neon (both tables, three indexes,
0 rows), code synced to `~/hermes-build`, uvicorn restarted (HTTP 200), 149 tests pass on the box (Python
3.14.4). Three adversarial Codex review rounds (16 → 13 → 4 defects) closed before deploy.

The first live eval immediately earned its keep: it found that SearXNG's upstream engines suspend
under the new query volume and return an empty result set that looks exactly like "nothing exists"
(lessons #22). Fixed and redeployed. Pacing, backoff-retry, and a note on `research_runs.notes`.

Code is on GitHub (**private**): `zaidkilledar21-beep/hermes-research`.

## Next real action

1. **Re-run the PCAC question** (run 20 died to a parse bug that is now fixed). The first real
   end-to-end test of the new targeting. Held so far because the owner asked for no research runs
   beyond testing.
2. **Decide on the SearXNG throttling ceiling.** Pacing + backoff makes throttling VISIBLE and
   survivable, not absent. The box's datacenter IP gets flagged by Google CSE/DuckDuckGo within a
   few dozen queries. The real fix is routing SearXNG through the residential proxy reach already
   uses (DataImpulse, $5/5GB, pay-as-you-go). That is a cost decision, deliberately left to the
   owner. Symptom to watch: `[discover] ... THROTTLED search` in the logs, or a note on a run.
3. **Widen the eval labels using live data.** First live discovery scored `reach` 0.0-0.17 across
   the set. The venues discovery actually returns are mostly not the ones hand-listed in `questions.json`.
   Read `evals/report-*.json` sample URLs and decide, per question, whether the labels were too narrow or the
   engine really is missing the practitioner venues. Do NOT relabel to make the number go up; the
   point of the metric is that it can go down.

Note the registry does nothing on its first runs by design. A venue needs answering evidence in two
DISTINCT runs on a topic before it earns priority. Expect early runs to look like today's plus
sharper queries; venue memory shows up after that.

Older robustness passes still open and unblocked: resource check under real load, kill-switch drill,
injection re-verify, full walled E2E.

## If picking this up cold, useful entry points
- Console: research.example.com (Cloudflare Access. Owner's email, one-time code)
- VPS: `ssh trader@203.0.113.10`
- Live services on the box: `docker ps`. Expect `hermes`, `reviewer`, `reach`, **`searxng`**
  containers + `uvicorn` (web app) + `cloudflared` + `caddy` as bare processes. Bare processes are
  supervised by `deploy/watchdog.sh` (cron, every minute); containers use `--restart unless-stopped`.
- Secrets: `/home/trader/hermes-build/.env` (main), `.burner-creds.env` (burner login), both
  chmod 600, server-only, never in the repo. The Hermes model key ALSO lives inside the container at
  `/opt/data/config.yaml` (root-owned, chmod 600, only reachable via `docker exec`).
- **Rotating the OpenRouter key:** `nano /home/trader/.newkey` (paste), then
  `/home/trader/rotate-openrouter.sh`. It updates the container config + all three `.env` entries, shreds the key file and
  the `.env` backup, restarts services, and verifies the old key is purged. A literal key was
  committed to git once (lessons.md #18), never put one in a tracked file.

## Housekeeping reminders for whoever (human or Claude) picks this up
- Read `docs/lessons.md` before touching reach/reviewer/Camoufox/Hermes-tunnel code again. Several
of those bugs are easy to reintroduce.
- Keep these four files current going forward. This handoff was badly stale for a stretch mid-build;
don't let that happen again.
