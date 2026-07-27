"""Debug: what does IG serve the logged-in burner session? Selectors + session-validity check."""
import os
from camoufox.sync_api import Camoufox

server = os.environ["REACH_PROXY_SERVER"]; user = os.environ["REACH_PROXY_USER"]
country = os.environ.get("REACH_PROXY_COUNTRY")
proxy = {"server": server, "username": f"{user}__cr.{country}" if country else user,
         "password": os.environ.get("REACH_PROXY_PASS", "")}

with Camoufox(headless=True, humanize=True, proxy=proxy, geoip=True) as b:
    ctx = b.new_context(storage_state="/home/reach/state/instagram.json")
    p = ctx.new_page()
    p.goto("https://www.instagram.com/explore/tags/coffee/", wait_until="domcontentloaded", timeout=45000)
    p.wait_for_timeout(6000)
    print("final_url:", p.url)
    print("title:", p.title())
    for sel in ["article", "a[href*='/p/']", "a[href*='/reel/']", "img[alt]", "div[role=button]"]:
        try:
            print(f"  {sel}: {len(p.query_selector_all(sel))}")
        except Exception as e:
            print(f"  {sel}: ERR {e}")
    body = p.query_selector("body")
    print("=== visible text (400) ===")
    print((body.inner_text() if body else "")[:400])
