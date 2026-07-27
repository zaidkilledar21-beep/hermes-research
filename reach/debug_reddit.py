"""Debug 3: capture the exact visible text www.reddit serves the VPS."""
from camoufox.sync_api import Camoufox

with Camoufox(headless=True, humanize=True) as b:
    p = b.new_page()
    p.goto("https://www.reddit.com/search/?q=claude%20code", wait_until="networkidle", timeout=45000)
    p.wait_for_timeout(6000)
    print("final_url:", p.url)
    print("shreddit-post:", len(p.query_selector_all("shreddit-post")))
    body = p.query_selector("body")
    print("=== visible text (first 800) ===")
    print((body.inner_text() if body else "")[:800])
