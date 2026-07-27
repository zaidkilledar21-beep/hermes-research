# Phase 2: comment-level Reddit thread reader (community anecdote)

## Objective
The engine currently scrapes Reddit's own SEARCH page, yielding post titles + snippets. The
practitioner insight ("we used X, they lost our inventory") lives in the REPLIES, which are never
fetched. Reddit's search is also unusable (returns anime/AITAH for niche queries) and its `.json`
API is IP-blocked from this proxy. Discovery is now done elsewhere via a search engine; this task
builds the READER that turns a list of thread URLs into **comment-level** evidence records.

Independence is the whole value of anecdote: 10 comments in one thread must never be mistakable for
10 independent reports. So every record carries author/thread identity.

## Files to MODIFY (only this one)
- `reach/reach_camoufox.py`

## Files you MUST NOT TOUCH
Everything else. Specifically: `pipeline/*`, `collectors/*`, `web/*`, `db/*`, `reviewer/*`, `tests/*`, `deploy/*`,
`.collab/*`. Do NOT write migrations, do NOT deploy, do NOT ssh, do NOT rebuild docker images.

## Verified facts you can rely on (already tested live: do not re-litigate)
- `old.reddit.com` renders fine through this container's proxy; `www.reddit.com/...json` and
`old.reddit.com/...json` both return "blocked by network security". **HTML only.**
- On a real thread page, comment bodies are reachable via `div.commentarea div.entry div.md`
(verified: 11 comments extracted).
- Existing helpers in this file you should reuse: `_browser()`, `_page()`, `_strip_chrome()`,
  `_wait_past_challenge()`. Existing readers (`read_forum`, `read_trustpilot`, `read_stackexchange`, `read_instagram`, `read_facebook`) must keep working
  unchanged.

## Required behaviour

### 1. New reader: `read_reddit_threads(urls, limit)`
Signature must accept a LIST of thread URLs and read them all in ONE browser session. Rationale: the
VPS has 4GB; launching a fresh Camoufox per thread is the main scale risk. Open the browser once,
reuse it across threads, close contexts between threads, and cap total threads read.

For each URL:
- Normalise to `old.reddit.com` (rewrite `www.reddit.com` / `reddit.com` / `np.reddit.com` host).
- Append a sort parameter so we do not only ever see early consensus. Read each thread once, but
  rotate the sort across the URL list: `?sort=top`, `?sort=new`, `?sort=controversial` (round-robin by index). Record
  which sort was used.
- Extract the POST itself (title + selftext + author + score + permalink) as one record with
`kind="post"`.
- Extract up to `limit` COMMENTS as separate records with `kind="comment"`.

### 2. Return shape (STRICT: the ingest side is written against this)
Return `list[dict]`, each dict EXACTLY these keys (use `None` when genuinely unavailable): ```
{
  "kind":         "post" | "comment",
  "url":          absolute permalink to the post or comment,
  "text":         cleaned body text (post selftext, or comment body),
  "author":       reddit username without "u/" prefix, or None if deleted/unavailable,
  "thread_id":    reddit base36 id of the THREAD (same for every record from one thread),
  "thread_title": the post title (same for every record from one thread),
  "comment_id":   base36 id of this comment, or None for the post record,
  "parent_id":    base36 id of the parent comment, or None if top-level/post,
  "score":        integer score if parseable, else None,
  "sort":         which sort was used for this thread read,
  "depth":        integer nesting depth (post = 0, top-level comment = 1, ...)
}
```
- Apply `_strip_chrome()` to text. Skip records whose text is empty, `[deleted]`, or `[removed]`.
- Cap each `text` at 2000 chars.
- Never raise on a single bad thread. Log to stderr and continue to the next URL. Return whatever
succeeded.

### 3. Selector guidance (old.reddit.com)
- thread id: from the URL path `/comments/<thread_id>/`
- post title: `a.title` or `p.title a`, post body: `div.expando div.md` / `div.usertext-body div.md`
- comments live under `div.commentarea`; each comment is `div.comment` (has `data-fullname`
like `t1_xxxxx`, `data-author`, and often `data-permalink`)
- comment body: `div.entry div.md` within that comment
- score: `span.score.unvoted` (text like "12 points"); parse the integer, else None
- depth: count ancestor `div.comment` elements, or use nesting of `div.child`
Use defensive `query_selector` checks. Reddit markup varies. If a field cannot be found, return `None` for
it rather than failing the record.

### 4. Wire it into `READERS`
- Add key `reddit_threads` → a wrapper the poller can call. The poller passes a request dict; support
  BOTH shapes: `req["urls"]` (list, preferred) and `req["query"]` (a single URL or a JSON array string, as
  fallback).
- Modify `handle()` minimally so a request carrying `urls` reaches this reader. Keep every other
source's existing behaviour byte-identical.
- **Retire the old search-based `read_reddit`**: keep the `reddit_reach` key working but have it
  return `[]` with a stderr note that Reddit search is retired in favour of `reddit_threads` (do not
  delete the function outright. Other code may still reference the key).

## Constraints
- Python 3, stdlib + the already-imported Camoufox/Playwright API only. No new dependencies.
- Match existing style: module docstring already documents sources. Update it. Comments explain WHY.
- No emoji. Do not reformat unrelated code.
- This process is READ-ONLY: never log in, never post, never click anything that mutates state.

## Verification you must run before reporting done
- `python -m py_compile reach/reach_camoufox.py`
- Write and run a tiny offline sanity check of any pure helper you add (e.g. URL normalisation,
  score parsing, base36 id extraction). Put it in `reach/_selftest_reddit.py`, run it, then DELETE the file before
  finishing. Do not add it to the repo permanently.
- You CANNOT test against live Reddit from here (no browser/proxy in this environment). Do not try.
Report which selectors you used and any field you were unsure about.
