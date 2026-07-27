"""Mission Control — shell + shared render helpers. Server-rendered, no template engine
(keeps deploy simple); CSS/JS live in web/static and are served via StaticFiles."""
from __future__ import annotations
import html

# simple stroke icons (inline SVG, currentColor)
_ICONS = {
    "overview": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>',
    "research": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>',
    "chat": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M21 12a8 8 0 0 1-11.5 7.2L3 21l1.8-6.5A8 8 0 1 1 21 12Z"/></svg>',
    "services": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/></svg>',
    "cost": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 3v18h18"/><path d="M7 15l3-4 3 2 5-6"/></svg>',
}
_NAV = [("overview", "/", "Overview"), ("research", "/research", "Research"),
        ("chat", "/chat", "Chat"), ("services", "/services", "Services"), ("cost", "/cost", "Cost")]


def shell(active: str, title: str, body: str, *, kill: bool = True) -> str:
    nav = "".join(
        f'<a class="navlink{" on" if key==active else ""}" href="{href}">{_ICONS[key]}<span>{label}</span></a>'
        for key, href, label in _NAV)
    killbtn = ('<button class="kill" data-action="/api/kill" '
               'data-confirm="Kill switch: stop the Hermes agent and any in-flight research. '
               'Data is preserved. Continue?">■ Kill</button>') if kill else ""
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{html.escape(title)} · Mission Control</title>
<link rel=stylesheet href="/static/app.css"></head><body>
<div class=shell>
  <aside class=rail>
    <div class=brand><div class=logo>S</div><div><b>Mission Control</b><span>synqro</span></div></div>
    {nav}
    <div class=spacer></div>
    <div class=foot>hy3 · minimax m3 · nemotron</div>
  </aside>
  <div class=main>
    <header class=topbar>
      <h1>{html.escape(title)}</h1>
      <div class="health-sum" data-health-sum><span class="dot ok live"></span>&nbsp;operational</div>
      <div class=right>{killbtn}</div>
    </header>
    <div class=content>{body}</div>
  </div>
</div>
<div class=toasts></div>
<script src="/static/app.js"></script></body></html>"""
