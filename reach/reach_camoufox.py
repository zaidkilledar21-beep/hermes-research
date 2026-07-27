"""Camoufox-based walled-source reader — the ONLY process in the isolated reach container.

Two modes:
  (default) poller  — watch /app/dropbox/req for scrape requests, run READ-ONLY Camoufox reads,
                      drop results to /app/dropbox/out. Hermes ingests + tags UNTRUSTED_EVIDENCE.
  login <platform>  — one-time interactive login for instagram/facebook via a virtual display + VNC;
                      saves the burner session to /home/reach/state/<platform>.json for later headless reads.

Sources:
  reddit_threads      comment-level old.reddit.com thread reader — NO login, HTML only.
  reddit_reach        retired Reddit search reader; retained as an empty compatibility source.
  stackexchange_reach network-wide Stack Exchange search — NO login.
  trustpilot_reach    independent reviews for a business domain — NO login.
  forum_reach         generic forum/thread reader (vBulletin/Discourse/phpBB), query = full URL.
  instagram_reach     logged-in hashtag explore — requires a saved burner session.
  facebook_reach      logged-in post search — requires a saved burner session.
The login-free readers just need the residential proxy to clear datacenter-IP blocks.

This process holds no real credential, never posts, and only ever READS.
"""
from __future__ import annotations
import json
import os
import pathlib
import re
import sys
import time

STATE = pathlib.Path("/home/reach/state")
DROP = pathlib.Path("/app/dropbox")
REQ = DROP / "req"
OUT = DROP / "out"
POLL_SECONDS = 10
MAX_ITEMS = 25

# Cheap first-pass noise filter: site chrome (nav/login/footer boilerplate) that shows up as its
# own line regardless of which forum/site engine rendered it. Deliberately generic — not tuned to
# any one site — so it doesn't overfit. The deep semantic cleanup is Nemotron's job downstream
# (pipeline/extract.py); this just keeps the obvious junk out of storage in the first place.
_CHROME_LINE = re.compile(
    r"(?i)^\s*(home|forums?|log ?in|register|search( forums)?|new posts|what'?s new|sticky|"
    r"change width|contact us|terms( of)? (service|use)|privacy policy|help|rss|next|previous|"
    r"filters?|cookie policy|accept cookies|skip to (main )?content|advertisement)\s*$"
)


def _strip_chrome(text: str) -> str:
    """Drop lines that are pure site-chrome boilerplate; keep everything else untouched."""
    if not text:
        return text
    lines = [ln for ln in text.splitlines() if ln.strip() and not _CHROME_LINE.match(ln.strip())]
    return "\n".join(lines).strip()


def _proxy() -> dict | None:
    """Residential proxy from env — required to get past platforms' datacenter-IP network blocks.
    REACH_PROXY_SERVER=http://host:port (+ optional REACH_PROXY_USER / REACH_PROXY_PASS).
    REACH_PROXY_COUNTRY (e.g. 'us') pins the exit country so the burner always looks like it logs
    in from the SAME place — random-country-per-session is a bigger abuse-detection flag than any
    single location. DataImpulse: append '__cr.<code>' to the username."""
    server = os.environ.get("REACH_PROXY_SERVER")
    if not server:
        return None
    p = {"server": server}
    user = os.environ.get("REACH_PROXY_USER")
    if user:
        country = os.environ.get("REACH_PROXY_COUNTRY")
        p["username"] = f"{user}__cr.{country}" if country else user
        p["password"] = os.environ.get("REACH_PROXY_PASS", "")
    return p


def _browser(headless: bool = True):
    """Launch-level Camoufox browser. Proxy is a LAUNCH param (applies to the whole browser);
    storage_state is a CONTEXT param — see _page(), don't pass it here."""
    from camoufox.sync_api import Camoufox
    proxy = _proxy()
    extra = {"proxy": proxy, "geoip": True} if proxy else {}
    return Camoufox(headless=headless, humanize=True, **extra)


def _page(browser, storage_state: str | None = None):
    """New page, optionally resuming a saved burner session (cookies/localStorage)."""
    ctx = browser.new_context(storage_state=storage_state) if storage_state else browser.new_context()
    return ctx, ctx.new_page()


# ── platform readers ──────────────────────────────────────────────────────────
def read_reddit(query: str, limit: int) -> list[dict]:
    """Compatibility shim for the retired, low-relevance Reddit search reader."""
    print("[reach] reddit_reach search is retired; use reddit_threads instead", file=sys.stderr)
    return []


_REDDIT_SORTS = ("top", "new", "controversial")
_REDDIT_HOSTS = {"reddit.com", "www.reddit.com", "np.reddit.com", "old.reddit.com"}


def _normalise_reddit_url(url: str, sort: str | None = None) -> str:
    """Use old.reddit.com HTML and optionally replace the thread's sort query parameter."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    raw = (url or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in ("http", "https") or parsed.hostname is None:
        raise ValueError("Reddit thread URL must be an absolute HTTP(S) URL")
    host = parsed.hostname.lower()
    if host not in _REDDIT_HOSTS:
        raise ValueError(f"unsupported Reddit host: {host}")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if sort is not None:
        query = [(key, value) for key, value in query if key.lower() != "sort"]
        query.append(("sort", sort))
    return urlunsplit(("https", "old.reddit.com", parsed.path or "/", urlencode(query), ""))


def _reddit_thread_id(url: str) -> str | None:
    """Extract the base36 thread id from /comments/<id>/ URLs."""
    from urllib.parse import urlsplit
    match = re.search(r"/comments/([0-9a-z]+)(?:/|$)", urlsplit(url).path, re.IGNORECASE)
    return match.group(1).lower() if match else None


def _reddit_base36_id(fullname: str | None, prefix: str) -> str | None:
    """Turn a Reddit fullname such as t1_ab12 into its validated base36 id."""
    value = (fullname or "").strip().lower()
    marker = prefix + "_"
    ident = value[len(marker):] if value.startswith(marker) else ""
    return ident if ident and re.fullmatch(r"[0-9a-z]+", ident) else None


def _reddit_score(text: str | None) -> int | None:
    """Parse old Reddit score text such as '12 points'; hidden/fuzzy scores remain unknown."""
    match = re.fullmatch(r"\s*(-?\d[\d,]*)(?:\s+points?)?\s*", text or "", re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _reddit_author(author: str | None) -> str | None:
    value = (author or "").strip()
    if value.lower().startswith("u/"):
        value = value[2:]
    return value if value and value.lower() not in {"[deleted]", "[removed]"} else None


def _reddit_text(text: str | None) -> str | None:
    value = _strip_chrome((text or "").strip())
    if not value or value.lower() in {"[deleted]", "[removed]"}:
        return None
    return value[:2000]


def _reddit_element_score(container) -> int | None:
    score = container.query_selector("span.score.unvoted") if container else None
    if not score:
        return None
    parsed = _reddit_score(score.inner_text())
    return parsed if parsed is not None else _reddit_score(score.get_attribute("title"))


def _reddit_element_author(container) -> str | None:
    if not container:
        return None
    author = container.get_attribute("data-author")
    if not author:
        author_el = container.query_selector("div.entry p.tagline a.author, a.author")
        author = author_el.inner_text() if author_el else None
    return _reddit_author(author)


def _reddit_thread_permalink(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    parsed = urlsplit(_normalise_reddit_url(url))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _reddit_permalink(base_url: str, value: str | None) -> str | None:
    from urllib.parse import urljoin
    return _normalise_reddit_url(urljoin(base_url, value), sort=None) if value else None


def read_reddit_threads(urls: list[str], limit: int) -> list[dict]:
    """Read post bodies and individual comments from old.reddit.com HTML in one browser session."""
    out: list[dict] = []
    comment_limit = max(0, int(limit))
    try:
        with _browser() as browser:
            # Bound thread count independently from the per-thread comment cap so a large discovery
            # batch cannot create an unbounded sequence of contexts on the 4GB VPS.
            for index, raw_url in enumerate(urls[:MAX_ITEMS]):
                ctx = None
                sort = _REDDIT_SORTS[index % len(_REDDIT_SORTS)]
                try:
                    url = _normalise_reddit_url(raw_url, sort)
                    thread_id = _reddit_thread_id(url)
                    if not thread_id:
                        raise ValueError("URL does not contain /comments/<thread_id>/")
                    ctx, page = _page(browser)
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    _wait_past_challenge(
                        page, ["div.commentarea", "div.thing[data-fullname^='t3_']",
                               "a.title", "p.title a"])
                    page.wait_for_timeout(2500)

                    post = (page.query_selector(f"div.thing[data-fullname='t3_{thread_id}']")
                            or page.query_selector("div.thing.link[data-fullname^='t3_']")
                            or page.query_selector("div.thing[data-fullname^='t3_']"))
                    title_el = ((post.query_selector("a.title, p.title a") if post else None)
                                or page.query_selector("a.title, p.title a"))
                    thread_title = _strip_chrome(
                        (title_el.inner_text() or "").strip()) if title_el else None
                    thread_title = thread_title or None

                    body_el = post.query_selector(
                        "div.expando div.md, div.usertext-body div.md") if post else None
                    post_text = _reddit_text(body_el.inner_text() if body_el else None)
                    post_link = post.get_attribute("data-permalink") if post else None
                    if not post_link and post:
                        comments_link = post.query_selector("a.comments")
                        post_link = comments_link.get_attribute("href") if comments_link else None
                    if post_text:
                        out.append({
                            "kind": "post",
                            "url": (_reddit_permalink(url, post_link)
                                    or _reddit_thread_permalink(url)),
                            "text": post_text,
                            "author": _reddit_element_author(post),
                            "thread_id": thread_id,
                            "thread_title": thread_title,
                            "comment_id": None,
                            "parent_id": None,
                            "score": _reddit_element_score(post),
                            "sort": sort,
                            "depth": 0,
                        })

                    comment_count = 0
                    comments = page.query_selector_all("div.commentarea div.comment")
                    for comment in comments:
                        if comment_count >= comment_limit:
                            break
                        body = comment.query_selector(":scope > div.entry div.md")
                        if not body:
                            body = comment.query_selector("div.entry div.md")
                        text = _reddit_text(body.inner_text() if body else None)
                        if not text:
                            continue
                        comment_id = _reddit_base36_id(
                            comment.get_attribute("data-fullname"), "t1")
                        parent_id = _reddit_base36_id(
                            comment.get_attribute("data-parent-fullname"), "t1")
                        depth = comment.evaluate(
                            """el => {
                                let depth = 1;
                                let parent = el.parentElement;
                                while (parent) {
                                    if (parent.matches && parent.matches("div.comment")) depth += 1;
                                    parent = parent.parentElement;
                                }
                                return depth;
                            }""")
                        permalink = comment.get_attribute("data-permalink")
                        if not permalink:
                            bylink = comment.query_selector(":scope > div.entry a.bylink")
                            permalink = bylink.get_attribute("href") if bylink else None
                        comment_url = _reddit_permalink(url, permalink)
                        if not comment_url and comment_id:
                            thread_url = _reddit_thread_permalink(url).rstrip("/") + "/"
                            comment_url = _reddit_permalink(thread_url, comment_id + "/")
                        out.append({
                            "kind": "comment",
                            "url": comment_url,
                            "text": text,
                            "author": _reddit_element_author(comment),
                            "thread_id": thread_id,
                            "thread_title": thread_title,
                            "comment_id": comment_id,
                            "parent_id": parent_id,
                            "score": _reddit_element_score(comment),
                            "sort": sort,
                            "depth": int(depth),
                        })
                        comment_count += 1
                except Exception as exc:
                    print(f"[reach] reddit_threads failed for {raw_url!r}: "
                          f"{type(exc).__name__}: {exc}", file=sys.stderr)
                finally:
                    if ctx is not None:
                        try:
                            ctx.close()
                        except Exception as exc:
                            print(f"[reach] reddit_threads context close failed for {raw_url!r}: "
                                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"[reach] reddit_threads browser session failed: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
    return out


def _read_reddit_threads_request(req: dict, limit: int) -> list[dict]:
    """Adapt the poller's preferred urls list and legacy query fallback to the thread reader."""
    value = req.get("urls") if isinstance(req.get("urls"), list) else req.get("query", "")
    if isinstance(value, list):
        urls = [url for url in value if isinstance(url, str) and url.strip()]
    elif isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            decoded = None
        urls = ([url for url in decoded if isinstance(url, str) and url.strip()]
                if isinstance(decoded, list) else ([value] if value.strip() else []))
    else:
        urls = []
    return read_reddit_threads(urls, limit)


def _read_with_session(platform: str, url: str, item_selector: str, limit: int) -> list[dict]:
    """Generic logged-in read for IG/FB using a saved burner storage_state."""
    state_file = STATE / f"{platform}.json"
    if not state_file.exists():
        raise RuntimeError(f"{platform} not logged in — run: docker exec -it reach "
                           f"python reach_camoufox.py login {platform}")
    out = []
    with _browser() as b:
        ctx, page = _page(b, storage_state=str(state_file))
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)
        for el in page.query_selector_all(item_selector)[:limit]:
            text = (el.inner_text() or "").strip()
            href = el.query_selector("a")
            link = href.get_attribute("href") if href else None
            if text:
                out.append({"url": link, "text": text[:2000]})
        ctx.close()
    return out


def read_instagram(query: str, limit: int) -> list[dict]:
    """Logged-in hashtag explore. Posts are <a href='/p/…'> tiles wrapping an <img alt='…'>;
    IG auto-generates descriptive alt text (the readable content for research)."""
    state = STATE / "instagram.json"
    if not state.exists():
        raise RuntimeError("instagram not logged in — run: docker exec reach python "
                           "reach_camoufox.py login instagram")
    out, seen = [], set()
    with _browser() as b:
        ctx, page = _page(b, storage_state=str(state))
        page.goto(f"https://www.instagram.com/explore/tags/{query}/",
                  wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(6000)
        for a in page.query_selector_all("a[href*='/p/'], a[href*='/reel/']"):
            href = a.get_attribute("href")
            if not href or href in seen:
                continue
            seen.add(href)
            img = a.query_selector("img[alt]")
            alt = (img.get_attribute("alt") if img else "") or ""
            if alt.strip():
                out.append({"url": "https://www.instagram.com" + href, "text": alt.strip()[:600]})
            if len(out) >= limit:
                break
        ctx.close()
    return out


def read_facebook(query: str, limit: int) -> list[dict]:
    # NOTE: FB selectors finalized against the real logged-in DOM after burner login (see plan).
    return _read_with_session("facebook", f"https://www.facebook.com/search/posts?q={query}",
                              "div[role=article]", limit)


# ── generic community / review readers (no login; residential proxy handles IP blocks) ──────
_CHALLENGE_MARKERS = ("just a moment", "checking your browser", "security verification",
                      "verify you are human", "enable javascript and cookies",
                      "performing security verification")


def _wait_past_challenge(page, ready_selectors: list[str], max_ms: int = 18000) -> None:
    """Cloudflare/JS interstitials serve a placeholder first; Camoufox (stealth) usually clears
    them on its own within a few seconds. Poll until a real content selector appears OR the
    challenge text is gone OR we hit max_ms — instead of reading the placeholder too early."""
    waited, step = 0, 1500
    while waited < max_ms:
        for sel in ready_selectors:
            if page.query_selector(sel):
                return
        body = (page.inner_text("body") or "").strip().lower() if page.query_selector("body") else ""
        if body and not any(m in body[:600] for m in _CHALLENGE_MARKERS) and len(body) > 400:
            return  # real content is present and it's not the interstitial
        page.wait_for_timeout(step)
        waited += step


def _harvest(url: str, selectors: list[str], limit: int, settle_ms: int = 3000,
             title_sel: str | None = None) -> list[dict]:
    """Load a page and pull text from the first selector that matches anything.

    Waits past any bot-challenge interstitial first, then tries each selector in order; the first
    that yields nodes wins. Falls back to <main>/<body> text so a selector drift degrades to
    'coarse but non-empty'. Every reader here is READ-ONLY; output is UNTRUSTED_EVIDENCE downstream."""
    out: list[dict] = []
    with _browser() as b:
        ctx, page = _page(b)
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        _wait_past_challenge(page, selectors)
        page.wait_for_timeout(settle_ms)
        nodes = []
        for sel in selectors:
            nodes = page.query_selector_all(sel)
            if nodes:
                break
        if not nodes:  # last-resort: the main content region as one coarse blob
            main = page.query_selector("main") or page.query_selector("body")
            txt = _strip_chrome((main.inner_text() or "").strip() if main else "")
            # Don't hand back a bot-challenge page as if it were content.
            if txt and not any(m in txt.lower()[:600] for m in _CHALLENGE_MARKERS):
                out.append({"url": url, "text": txt[:4000]})
            ctx.close()
            return out
        for el in nodes[:limit]:
            text = _strip_chrome((el.inner_text() or "").strip())
            if not text:
                continue
            a = el.query_selector("a[href]")
            href = a.get_attribute("href") if a else None
            if href and href.startswith("/"):
                from urllib.parse import urljoin
                href = urljoin(url, href)
            out.append({"url": href or url, "text": text[:2000]})
        ctx.close()
    return out


def read_stackexchange(query: str, limit: int) -> list[dict]:
    """Network-wide Stack Exchange search via the OFFICIAL API (api.stackexchange.com) — clean JSON,
    no Cloudflare wall, no key needed at this volume. Scraping SO search hits a bot challenge, so we
    go straight to the API. Returns question titles + body excerpts (community-tier Q&A signal)."""
    import gzip
    import html as _html
    import json as _json
    import re as _re
    import urllib.request
    from urllib.parse import urlencode
    params = urlencode({"order": "desc", "sort": "relevance", "q": query, "site": "stackoverflow",
                        "filter": "withbody", "pagesize": min(limit, 25)})
    req = urllib.request.Request("https://api.stackexchange.com/2.3/search/advanced?" + params,
                                 headers={"Accept-Encoding": "gzip", "User-Agent": "hermes-research"})
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    tag = _re.compile(r"<[^>]+>")
    out = []
    for it in _json.loads(raw.decode("utf-8", "replace")).get("items", [])[:limit]:
        body = _html.unescape(tag.sub(" ", it.get("body", "") or "")).strip()
        title = _html.unescape(it.get("title", "") or "")
        text = f"{title}\n\n{body}".strip()
        if text:
            out.append({"url": it.get("link"), "text": text[:2000]})
    return out


def read_trustpilot(query: str, limit: int) -> list[dict]:
    """Independent review reader. `query` is the business domain (e.g. '3plguys.com') or its
    Trustpilot slug — the review page is /review/<domain>. Trustpilot is a Next.js app that embeds
    all reviews in a __NEXT_DATA__ script; we pull that structured JSON first (survives the JS shell),
    and fall back to DOM/text harvest if the shape shifts."""
    import json as _json
    q = query.strip()
    url = (f"https://www.trustpilot.com/review/{q}" if "." in q and " " not in q
           else f"https://www.trustpilot.com/search?query={q.replace(' ', '+')}")
    ready = ["#__NEXT_DATA__", "[data-service-review-text-typography]", "article"]
    with _browser() as b:
        ctx, page = _page(b)
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        _wait_past_challenge(page, ready)
        page.wait_for_timeout(2500)
        out = []
        nd = page.query_selector("#__NEXT_DATA__")
        if nd:
            try:
                data = _json.loads(nd.inner_text())
                reviews = _find_reviews(data)
                for rv in reviews[:limit]:
                    txt = (rv.get("text") or rv.get("title") or "").strip()
                    if txt:
                        out.append({"url": url, "text": txt[:2000]})
            except Exception:
                pass
        ctx.close()
    if out:
        return out
    # fallback: DOM/text harvest (challenge-aware) if __NEXT_DATA__ wasn't usable
    return _harvest(url, ["[data-service-review-text-typography]", "article[data-review-card]",
                          "section article", "article"], limit, settle_ms=3500)


def _find_reviews(obj) -> list[dict]:
    """Walk Trustpilot's __NEXT_DATA__ blob for review objects (shape drifts across releases, so
    we hunt for dicts that look like a review: a text/title body with a rating)."""
    found = []

    def walk(o):
        if isinstance(o, dict):
            if ("text" in o or "title" in o) and ("rating" in o or "stars" in o or "score" in o):
                found.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return found


def read_forum(query: str, limit: int) -> list[dict]:
    """Generic forum/thread reader. `query` is a FULL URL to a forum thread or search page
    (vBulletin like MESO-Rx, Discourse, phpBB, etc.). Pulls post bodies via the common
    containers those engines use, degrading to page text if none match."""
    if not query.startswith("http"):
        raise ValueError("forum_reach expects a full thread/search URL as its query")
    return _harvest(
        query,
        ["div.post_message", "[id^=post_message_]",      # vBulletin
         "div.post .cooked", "div.topic-body .cooked",   # Discourse
         "div.postbody .content", ".post .content",      # phpBB / generic
         "article", "div.post"],
        limit, settle_ms=3500)


READERS = {"reddit_reach": read_reddit, "reddit_threads": _read_reddit_threads_request,
           "instagram_reach": read_instagram,
           "facebook_reach": read_facebook, "stackexchange_reach": read_stackexchange,
           "trustpilot_reach": read_trustpilot, "forum_reach": read_forum}


# ── poller ────────────────────────────────────────────────────────────────────
def handle(req_path: pathlib.Path) -> None:
    try:
        req = json.loads(req_path.read_text(encoding="utf-8"))
    except Exception as e:
        _write_out(req_path.stem, None, {"error": f"bad request: {e}"}); req_path.unlink(missing_ok=True)
        return
    source = req.get("source"); reader = READERS.get(source)
    rid = req.get("id", req_path.stem)
    if not reader:
        _write_out(rid, req.get("run_id"), {"source": source, "error": "unknown source"})
    else:
        try:
            reader_arg = req if source == "reddit_threads" else req.get("query", "")
            items = reader(reader_arg, int(req.get("limit", MAX_ITEMS)))
            _write_out(rid, req.get("run_id"), {"source": source, "query": req.get("query"),
                                                "raw": json.dumps(items)})
        except Exception as e:
            _write_out(rid, req.get("run_id"), {"source": source, "error": f"{type(e).__name__}: {e}"})
    req_path.unlink(missing_ok=True)


def _write_out(rid, run_id, payload: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{rid}.json").write_text(json.dumps({"id": rid, "run_id": run_id, **payload}),
                                     encoding="utf-8")


def poll() -> None:
    REQ.mkdir(parents=True, exist_ok=True); OUT.mkdir(parents=True, exist_ok=True)
    print("[reach] camoufox poller up; sources:", ", ".join(READERS), flush=True)
    while True:
        for req_path in sorted(REQ.glob("*.json")):
            handle(req_path)
        time.sleep(POLL_SECONDS)


# ── one-time browser-based login for IG/FB ────────────────────────────────────
# Runs Camoufox HEADFUL on a virtual display, streamed to the owner's browser via noVNC (port 6080),
# THROUGH the residential proxy — so the burner session is created on the same residential IP the
# headless reads use later. The owner logs in + clears any challenge, then a done-signal file is
# dropped (touch /tmp/login-done via docker exec) and the session is saved for headless reuse.
import subprocess


def login(platform: str) -> None:
    url = {"instagram": "https://www.instagram.com/accounts/login/",
           "facebook": "https://www.facebook.com/login/"}.get(platform)
    if not url:
        print(f"login not needed / unsupported for {platform}"); return
    STATE.mkdir(parents=True, exist_ok=True)
    done = pathlib.Path("/tmp/login-done")
    done.unlink(missing_ok=True)

    # virtual display + VNC + noVNC web bridge — started fresh each login, killed on exit below.
    procs = [
        subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1440x900x24"], stderr=subprocess.DEVNULL),
    ]
    time.sleep(2)
    os.environ["DISPLAY"] = ":99"
    procs.append(subprocess.Popen(["x11vnc", "-display", ":99", "-nopw", "-listen", "0.0.0.0",
                 "-rfbport", "5900", "-forever", "-shared", "-quiet"], stderr=subprocess.DEVNULL))
    procs.append(subprocess.Popen(["websockify", "--web=/usr/share/novnc", "6080", "localhost:5900"],
                 stderr=subprocess.DEVNULL))
    time.sleep(2)
    print(f"[reach] {platform} login browser is up. Open the noVNC URL, log into the BURNER, clear any")
    print("        challenge, then signal done:  docker exec reach touch /tmp/login-done")

    try:
        with _browser(headless=False) as b:
            ctx, page = _page(b)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            while not done.exists():        # wait for the owner to finish in the browser
                time.sleep(2)
            ctx.storage_state(path=str(STATE / f"{platform}.json"))
            ctx.close()
        done.unlink(missing_ok=True)
        print(f"[reach] saved {platform} session → state/{platform}.json. Headless reads reuse it.")
    finally:
        for p in procs:      # always kill Xvfb/x11vnc/websockify, even on error
            p.terminate()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "login":
        login(sys.argv[2])
    else:
        poll()
