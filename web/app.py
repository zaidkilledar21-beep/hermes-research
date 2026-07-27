"""Mission Control — unified console for the Hermes/research system (FastAPI).
Served on 127.0.0.1:8080; Cloudflare Tunnel + basic-auth (+ Cloudflare Access) front it.
Panels: Overview / Research / Chat / Services / Cost. Run:
  uvicorn web.app:app --host 127.0.0.1 --port 8080   (from repo root, env loaded)
"""
from __future__ import annotations
import html
import os
import secrets
import shutil
import subprocess
import sys
import psycopg
import requests
from fastapi import FastAPI, Form, Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from web.ui import shell

DATABASE_URL = os.environ["DATABASE_URL"]
SOURCES = ["web_search", "reddit_threads", "x", "github", "web", "rss", "youtube", "hackernews",
           "stackexchange_reach", "trustpilot_reach", "forum_reach", "instagram_reach",
           "facebook_reach"]
# reddit_reach (Reddit's own search) is retired — its relevance was unusable (anime/AITAH results
# for niche B2B queries) and it only ever returned post titles. reddit_threads replaces it:
# discovery via search engine, then comment-level reading of the actual threads.
# The floor is every source that takes a free-text SEARCH query — these work on any question with
# no extra input. web/rss/youtube/trustpilot_reach/forum_reach are deliberately excluded: they take
# a URL/feed/domain, not a question, so forcing them on every run guarantees a failed fetch (this
# WAS a bug — `web` hit Jina with an entire paragraph as if it were a page URL and 400'd on every
# single run). facebook_reach excluded too (burner suspended, non-functional). Hermes can still add
# any of the URL-shaped ones explicitly when the question actually gives it something to point at.
# web_search leads — it's the open-web spine, the highest-value always-on source.
ALWAYS_SOURCES = ["web_search", "reddit_threads", "x", "github", "hackernews",
                  "stackexchange_reach", "instagram_reach"]
# "extracting" was missing, so a run mid-extraction fell through to the report renderer and showed
# an empty report instead of a working indicator.
WORKING = {"decomposing", "collecting", "extracting", "synthesizing", "reviewing"}
HERE = os.path.dirname(__file__)

app = FastAPI(title="Mission Control")
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

# Gating is now done at the edge by Cloudflare Access (email-OTP, owner@example.com only) on
# both research.example.com and chat.example.com. The app itself is loopback-only (never bound to
# a public interface) — the tunnel is the sole path in, and Access guards the tunnel. App-level basic
# auth is therefore redundant friction and has been dropped; auth() stays as a no-op dependency so
# every route keeps its explicit auth marker and re-enabling a second layer later is a one-line change.
_security = HTTPBasic(auto_error=False)
_WEB_USER = os.environ.get("HERMES_WEB_USER", "zaid")
_WEB_PASS = os.environ.get("HERMES_WEB_PASS", "")


def auth(creds: HTTPBasicCredentials | None = Depends(_security)) -> bool:
    return True


# ── system probes (run as trader, in docker group) ──────────────────────────
def _sh(cmd: list[str], timeout: int = 5) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def _svc_status() -> list[dict]:
    out = []
    # web app = us, we're serving
    out.append({"name": "web", "label": "Console / web app", "status": "up", "detail": "serving :8080"})
    # cloudflared tunnel
    tun = _sh(["pgrep", "-x", "cloudflared"])
    out.append({"name": "tunnel", "label": "Cloudflare tunnel", "status": "up" if tun else "down",
                "detail": "connected" if tun else "not running"})
    # hermes container
    running = _sh(["docker", "inspect", "-f", "{{.State.Running}}", "hermes"])
    out.append({"name": "hermes", "label": "Hermes agent", "status": "up" if running == "true" else "down",
                "detail": "container up" if running == "true" else "container stopped"})
    # neon db
    db = "down"
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=4) as c:
            c.execute("SELECT 1"); db = "up"
    except Exception:
        db = "down"
    out.append({"name": "db", "label": "Neon database", "status": db, "detail": "reachable" if db == "up" else "unreachable"})
    # reach container (walled sources) — not deployed yet
    out.append({"name": "reach", "label": "Reach scraper", "status": "idle", "detail": "not enabled"})
    return out


def _sysinfo() -> dict:
    d = {"mem_used": "?", "mem_total": "?", "mem_pct": 0, "disk_used": "?", "disk_total": "?", "disk_pct": 0}
    mem = _sh(["free", "-m"])
    for line in mem.splitlines():
        if line.lower().startswith("mem:"):
            p = line.split()
            total, used = int(p[1]), int(p[2])
            d.update(mem_total=f"{total/1024:.1f}G", mem_used=f"{used/1024:.1f}G",
                     mem_pct=round(used / total * 100) if total else 0)
    try:
        t, u, _f = shutil.disk_usage("/")
        d.update(disk_total=f"{t/1e9:.0f}G", disk_used=f"{u/1e9:.0f}G", disk_pct=round(u / t * 100))
    except Exception:
        pass
    return d


def _cost() -> dict:
    try:
        with psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=4) as c:
            total = c.execute("SELECT COALESCE(SUM(cost_usd),0) FROM agent_runs").fetchone()[0]
            today = c.execute("SELECT COALESCE(SUM(cost_usd),0) FROM agent_runs "
                              "WHERE created_at::date = current_date").fetchone()[0]
            runs = c.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0]
        return {"cost_total": f"{float(total):.4f}", "cost_today": f"{float(today):.4f}", "runs_total": runs}
    except Exception:
        return {"cost_total": "0", "cost_today": "0", "runs_total": 0}


def _recent_rows(limit: int = 8) -> list[tuple]:
    try:
        with psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=4) as c:
            return c.execute("SELECT run_id, question, status FROM research_runs "
                             "ORDER BY run_id DESC LIMIT %s", (limit,)).fetchall()
    except Exception:
        return []


# ── OVERVIEW ─────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def overview(_: bool = Depends(auth)) -> str:
    svcs = _svc_status(); sysi = _sysinfo(); cost = _cost(); rows = _recent_rows(6)
    dot = lambda s: "ok live" if s == "up" else "warn" if s == "degraded" else "bad" if s == "down" else "idle"
    svc_html = "".join(
        f'<div class=srow><span class="dot {dot(s["status"])}" data-svc="{s["name"]}" title="{html.escape(s["detail"])}"></span>'
        f'<div class=grow><div class=name>{html.escape(s["label"])}</div>'
        f'<div class=meta>{html.escape(s["detail"])}</div></div></div>' for s in svcs)
    act = "".join(
        f'<a class=act-item href="/run/{r[0]}"><span class=n>#{r[0]}</span>'
        f'<span class=q>{html.escape((r[1] or "")[:70])}</span>{_status_badge(r[2])}</a>'
        for r in rows) or '<p class=sub>No research runs yet.</p>'
    body = f"""
    <div data-overview></div>
    <div class="grid g-3">
      <div class=card><h3>Research spend · today</h3>
        <div class=stat><span class=unit>$</span><span data-cost-today>{cost['cost_today']}</span></div>
        <div class=sub>total <span data-cost-total>{cost['cost_total']}</span> · {cost['runs_total']} runs</div></div>
      <div class=card><h3>Memory</h3>
        <div class="stat sm"><span data-mem>{sysi['mem_used']} / {sysi['mem_total']}</span></div>
        <div class=meter><i data-mem-bar style="width:{sysi['mem_pct']}%"></i></div></div>
      <div class=card><h3>Disk</h3>
        <div class="stat sm"><span data-disk>{sysi['disk_used']} / {sysi['disk_total']}</span></div>
        <div class=meter><i data-disk-bar style="width:{sysi['disk_pct']}%"></i></div></div>
    </div>
    <div class="grid g-2" style=margin-top:1rem>
      <div class="card pad-lg"><h3 style=margin-bottom:.6rem>Services</h3>{svc_html}
        <div style=margin-top:.8rem><a class="btn sm ghost" href=/services>Manage services →</a></div></div>
      <div class="card pad-lg"><h3 style=margin-bottom:.6rem>Recent research</h3>{act}
        <div style=margin-top:.8rem><a class="btn sm ghost" href=/research>Open research →</a></div></div>
    </div>"""
    return shell("overview", "Overview", body)


@app.get("/api/overview")
def api_overview(_: bool = Depends(auth)):
    return JSONResponse({**_sysinfo(), **_cost(), "services": _svc_status()})


def _status_badge(status: str) -> str:
    if status == "delivered":
        return '<span class="badge ok">done</span>'
    if status == "gated":
        return '<span class="badge warn">gated</span>'
    return f'<span class="badge mut">{html.escape(status)}</span>'


# ── RESEARCH ─────────────────────────────────────────────────────────────────
def _pills() -> str:
    out = []
    for s in SOURCES:
        walled = " walled" if s.endswith("_reach") else ""
        # Everything defaults on except Facebook — burner's suspended, nothing to gain checking it.
        checked = "" if s == "facebook_reach" else " checked"
        label = s.replace("_reach", "").capitalize() + (" ⚠" if s.endswith("_reach") else "")
        out.append(f'<label class="pill{walled}"><input type=checkbox name=sources value={s}{checked}>{html.escape(label)}</label>')
    return "".join(out)


@app.get("/research", response_class=HTMLResponse)
def research(_: bool = Depends(auth)) -> str:
    rows = _recent_rows(15)
    recent = "".join(
        f'<a class=act-item href="/run/{r[0]}"><span class=n>#{r[0]}</span>'
        f'<span class=q>{html.escape((r[1] or "")[:80])}</span>{_status_badge(r[2])}</a>'
        for r in rows) or '<p class=sub>No runs yet.</p>'
    body = f"""
    <form method=post action=/ask>
      <textarea name=question placeholder="Ask anything — what are people saying about X, what do developers report about Y, how does A compare to B…" required autofocus></textarea>
      <div class=pills>{_pills()}</div>
      <button class="btn pri" type=submit>Research it</button>
    </form>
    <div class=eyebrow>Recent</div>
    <div class=card>{recent}</div>"""
    return shell("research", "Research", body)


@app.post("/ask")
def ask(question: str = Form(...), sources: list[str] = Form(default=["github", "web"]), _: bool = Depends(auth)):
    from pipeline.submit import request_run
    run_id = request_run(question, sources, by="web")
    subprocess.Popen([sys.executable, "-m", "pipeline.run", "--run", str(run_id)],
                     cwd=os.getcwd(), env=dict(os.environ),
                     stdout=open(f"/tmp/run-{run_id}.log", "w"), stderr=subprocess.STDOUT)
    return RedirectResponse(f"/run/{run_id}", status_code=303)


@app.get("/run/{run_id}", response_class=HTMLResponse)
def run_page(run_id: int, _: bool = Depends(auth)) -> str:
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        row = conn.execute("SELECT question, status, notes FROM research_runs WHERE run_id=%s", (run_id,)).fetchone()
    if not row:
        return shell("research", "Not found", '<p class=sub>Run not found.</p>')
    question, status, notes = row
    head = f'<a class="btn sm ghost" href=/research>← research</a><h2 class=page-h style=margin-top:1rem>{html.escape(question)}</h2>'
    if status in WORKING or status == "decomposing":
        return shell("research", f"Run {run_id}",
                     head + f'<p class=sub><span class="dot ok live"></span> Working — {html.escape(status)}. Refreshing…</p>'
                     '<meta http-equiv=refresh content=4>')
    if status == "gated":
        return shell("research", f"Run {run_id}",
                     head + f'<div class=card style="border-left:3px solid var(--warn)">'
                     f'<span class="badge warn">blocked</span><p>{html.escape(notes or "integrity checks failed")}</p></div>')
    return shell("research", f"Run {run_id}", head + _render_report(run_id))


def _render_report(run_id: int) -> str:
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        # Only accepted findings render as findings — quarantined ones (fabricated citations,
        # reviewer-rejected) must not appear here as if they were delivered results.
        findings = conn.execute("SELECT finding_id, claim, label, confidence, evidence_ids FROM findings "
                                "WHERE run_id=%s AND disposition='accepted' "
                                "ORDER BY array_length(evidence_ids,1) DESC NULLS LAST", (run_id,)).fetchall()
        ev = {r[0]: r for r in conn.execute(
            "SELECT evidence_id, url, grade, trust_tag, source_id, credibility_tier FROM evidence_items WHERE run_id=%s", (run_id,))}
        nev = conn.execute("SELECT COUNT(*) FROM evidence_items WHERE run_id=%s", (run_id,)).fetchone()[0]
        cost = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM agent_runs WHERE run_id=%s", (run_id,)).fetchone()[0]
    tone = {"observed": "ok", "inferred": "warn", "community_signal": "warn", "unknown": "mut"}
    edge = {"observed": "var(--ok)", "inferred": "var(--warn)",
            "community_signal": "var(--warn)", "unknown": "var(--idle)"}
    groups = [("observed", "Observed"), ("inferred", "Inferred"),
              ("community_signal", "Community signal (anecdotal / low-N)"), ("unknown", "Gaps")]
    tier_short = {"primary_authority": "authority", "reference": "reference",
                  "independent_review": "review", "vendor_marketing": "vendor-claim",
                  "community": "community", "general_web": "web", "user_supplied": "user-doc"}
    out = []
    for label, title in groups:
        rows = [f for f in findings if f[2] == label]
        if not rows:
            continue
        out.append(f'<div class=eyebrow>{title}</div>')
        for fid, claim, _l, conf, ev_ids in rows:
            chips = []
            for e in ev_ids or []:
                if e in ev:
                    _, url, grade, trust, src, tier = ev[e]
                    warn = trust == "UNTRUSTED_EVIDENCE"
                    t = tier_short.get(tier, tier or "web")
                    chips.append(f'<a class="badge {"bad" if warn else "mut"}" href="{html.escape(url or "#")}" target=_blank rel=noopener>'
                                 f'{html.escape(src)} · {t} · {grade}{" ⚠" if warn else ""}</a>')
            cf = f' · <span style=color:var(--warn)>{conf}</span>' if conf is not None else ""
            out.append(f'<div class=card style="border-left:3px solid {edge[label]};margin:.5rem 0">'
                       f'<span class="badge {tone[label]}">{label.upper()}</span>{cf}'
                       f'<p style=margin:.5rem_0>{html.escape(claim)}</p>'
                       f'<div style="display:flex;flex-wrap:wrap;gap:.35rem">{"".join(chips)}</div></div>')
    out.append(f'<p class=sub style=margin-top:1.5rem>{nev} evidence items · {len(findings)} findings · ${float(cost):.4f} · walled/⚠ scraped, unverified</p>')
    return "".join(out)


# ── JSON API (Hermes skill drives research through this) ─────────────────────
@app.post("/api/ask")
def api_ask(question: str = Form(...), sources: str = Form(default=""), _: bool = Depends(auth)):
    from pipeline.submit import request_run
    requested = [s.strip() for s in sources.split(",") if s.strip()]
    # ENFORCED floor, not a suggestion: every Hermes-driven run always includes ALWAYS_SOURCES,
    # regardless of what Hermes passed (or forgot to pass). Anything else Hermes explicitly asked
    # for (e.g. trustpilot_reach with a real domain) is unioned on top, never dropped.
    src = sorted(set(ALWAYS_SOURCES) | set(requested))
    run_id = request_run(question, src, by="hermes")
    subprocess.Popen([sys.executable, "-m", "pipeline.run", "--run", str(run_id)],
                     cwd=os.getcwd(), env=dict(os.environ),
                     stdout=open(f"/tmp/run-{run_id}.log", "w"), stderr=subprocess.STDOUT)
    return {"run_id": run_id, "status": "started", "poll": f"/api/run/{run_id}"}


@app.get("/api/run/{run_id}")
def api_run(run_id: int, _: bool = Depends(auth)):
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        row = conn.execute("SELECT question, status, report_md, notes FROM research_runs WHERE run_id=%s", (run_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="run not found")
    q, status, report_md, notes = row
    return {"run_id": run_id, "question": q, "status": status,
            "done": status in ("delivered", "gated"), "report_md": report_md, "notes": notes}


# ── Cross-run synthesis (Claude-draft -> Codex-critique -> Claude-revise via reviewer container) ──
@app.get("/api/runs")
def api_runs(limit: int = 30, _: bool = Depends(auth)):
    """Recent runs, so the synthesize-project skill can discover which delivered runs to consolidate."""
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT run_id, question, status FROM research_runs ORDER BY submitted_at DESC LIMIT %s",
            (min(limit, 100),)).fetchall()
    return {"runs": [{"run_id": r[0], "question": r[1], "status": r[2]} for r in rows]}


@app.post("/api/synthesize")
def api_synthesize(run_ids: str = Form(...), title: str = Form(default=""), _: bool = Depends(auth)):
    ids = sorted({int(x) for x in run_ids.replace(",", " ").split() if x.strip().isdigit()})
    if not ids:
        raise HTTPException(status_code=400, detail="no valid run_ids")
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        sid = conn.execute(
            "INSERT INTO cross_syntheses (run_ids, title, requested_by) VALUES (%s,%s,'hermes') "
            "RETURNING synthesis_id", (ids, title or None)).fetchone()[0]
    subprocess.Popen([sys.executable, "-m", "pipeline.cross_synthesize", "--synthesis", str(sid)],
                     cwd=os.getcwd(), env=dict(os.environ),
                     stdout=open(f"/tmp/synth-{sid}.log", "w"), stderr=subprocess.STDOUT)
    return {"synthesis_id": sid, "status": "synthesizing", "poll": f"/api/synthesis/{sid}",
            "run_ids": ids}


@app.get("/api/synthesis/{synthesis_id}")
def api_synthesis(synthesis_id: int, _: bool = Depends(auth)):
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        row = conn.execute(
            "SELECT run_ids, title, status, report_md FROM cross_syntheses WHERE synthesis_id=%s",
            (synthesis_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="synthesis not found")
    run_ids, title, status, report_md = row
    return {"synthesis_id": synthesis_id, "run_ids": list(run_ids), "title": title,
            "status": status, "done": status in ("delivered", "failed"), "report_md": report_md}


# ── CHAT / SERVICES / COST (scaffold — fleshed out next phase) ────────────────
@app.get("/chat", response_class=HTMLResponse)
def chat(_: bool = Depends(auth)) -> str:
    body = ("""
    <div class=card style="padding:0;overflow:hidden;height:calc(100vh - 160px);min-height:480px">
      <iframe src="https://chat.example.com" title="Hermes"
        style="width:100%;height:100%;border:0;display:block;background:var(--surface)"
        onerror="this.style.display='none';document.getElementById('chat-fallback').style.display='block'"></iframe>
    </div>
    <div id=chat-fallback class=card style="display:none;margin-top:1rem">
      <p class=sub>Couldn't embed Hermes here (it may block framing).</p>
      <a class="btn pri" href="https://chat.example.com" target=_blank>Open Hermes in a new tab ↗</a>
    </div>""")
    return shell("chat", "Chat", body, kill=True)


_RESTARTABLE = {"hermes": "restart-hermes", "tunnel": "restart-tunnel", "web": "restart-web"}
_LOG_SRC = {"hermes": ("docker", ["docker", "logs", "--tail", "40", "hermes"]),
            "web": ("file", "/tmp/web.log"), "tunnel": ("file", "/tmp/cf-tunnel.log")}


@app.get("/services", response_class=HTMLResponse)
def services(_: bool = Depends(auth)) -> str:
    svcs = _svc_status()
    dot = lambda s: "ok live" if s == "up" else "bad" if s == "down" else "idle"
    rows = []
    for s in svcs:
        act = ""
        if s["name"] in _RESTARTABLE:
            act = (f'<button class="btn sm" data-action="/api/service/{_RESTARTABLE[s["name"]]}" '
                   f'data-confirm="Restart {html.escape(s["label"])}?">↻ Restart</button>')
        logbtn = ('<button class="btn sm ghost" onclick="mcLogs(\'' + s["name"] + '\')">Logs</button>'
                  if s["name"] in _LOG_SRC else "")
        badge = f'<span class="badge {"ok" if s["status"]=="up" else "bad" if s["status"]=="down" else "mut"}">{s["status"]}</span>'
        rows.append(f'<div class=srow><span class="dot {dot(s["status"])}" data-svc="{s["name"]}"></span>'
                    f'<div class=grow><div class=name>{html.escape(s["label"])} {badge}</div>'
                    f'<div class=meta>{html.escape(s["detail"])}</div></div>'
                    f'<div class=acts>{logbtn}{act}</div></div>')
    body = f"""
    <p class=page-sub>Health and control for everything running on the box. Restarts are safe; the kill switch stops the agent but preserves all data.</p>
    <div class="card pad-lg">{''.join(rows)}</div>
    <div class=eyebrow>Danger zone</div>
    <div class=card style="border-color:oklch(66% .19 25 / .35)">
      <div class=srow style=border:0>
        <div class=grow><div class=name>Kill switch</div>
          <div class=meta>Stops the Hermes agent and any in-flight research. Neon data + evidence preserved. Console stays up.</div></div>
        <button class=kill data-action="/api/kill" data-confirm="Kill switch: stop Hermes + in-flight research. Data preserved. Continue?">■ Kill everything</button></div>
    </div>
    <div class=eyebrow>Logs</div>
    <div class=card><pre id=logout style="margin:0;white-space:pre-wrap;word-break:break-word;font:12px/1.5 var(--mono);color:var(--dim);max-height:340px;overflow:auto">Pick a service's “Logs” above.</pre></div>
    <script>
      async function mcLogs(svc){{const p=document.getElementById('logout');p.textContent='loading…';
        try{{const r=await fetch('/api/logs/'+svc);const t=await r.text();p.textContent=t||'(empty)';}}catch(e){{p.textContent='failed to load logs';}}}}
    </script>"""
    return shell("services", "Services", body)


@app.post("/api/service/{action}")
def api_service(action: str, _: bool = Depends(auth)):
    if action == "restart-hermes":
        _sh(["docker", "restart", "hermes"], timeout=30)
        return {"ok": True, "message": "Hermes restarted."}
    if action == "restart-tunnel":
        _sh(["pkill", "-x", "cloudflared"])
        subprocess.Popen(["bash", "-c", "sleep 1; setsid --fork /home/trader/bin/cloudflared tunnel run research "
                          ">/tmp/cf-tunnel.log 2>&1 </dev/null"])
        return {"ok": True, "message": "Tunnel restarting…"}
    if action == "restart-web":
        subprocess.Popen(["bash", "-c", "sleep 1; fuser -k 8080/tcp; sleep 1; "
                          "setsid --fork /home/trader/hermes-build/start-web.sh >/tmp/web.log 2>&1 </dev/null"])
        return {"ok": True, "message": "Console restarting — this page will blip, then reconnect."}
    raise HTTPException(status_code=400, detail="unknown action")


@app.get("/api/logs/{svc}")
def api_logs(svc: str, _: bool = Depends(auth)):
    from fastapi.responses import PlainTextResponse
    src = _LOG_SRC.get(svc)
    if not src:
        raise HTTPException(status_code=404, detail="no logs")
    kind, ref = src
    if kind == "docker":
        return PlainTextResponse(_sh(ref, timeout=8) or "(no output)")
    try:
        with open(ref, encoding="utf-8", errors="replace") as f:
            return PlainTextResponse("".join(f.readlines()[-40:]) or "(empty)")
    except Exception:
        return PlainTextResponse("(log not found)")


def _openrouter_balance() -> dict:
    key = os.environ.get("OPENROUTER_API_KEY_ANALYST", "")
    if not key:
        return {}
    try:
        r = requests.get("https://openrouter.ai/api/v1/credits",
                         headers={"Authorization": f"Bearer {key}"}, timeout=8)
        d = r.json().get("data", {})
        total = float(d.get("total_credits", 0)); used = float(d.get("total_usage", 0))
        return {"granted": total, "used": used, "remaining": max(0, total - used)}
    except Exception:
        return {}


@app.get("/cost", response_class=HTMLResponse)
def cost(_: bool = Depends(auth)) -> str:
    cap = float(os.environ.get("OPENROUTER_DAILY_CAP_USD", "0.5"))
    c = _cost(); bal = _openrouter_balance()
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        by_model = conn.execute(
            "SELECT COALESCE(model_actual,'?'), COUNT(*), COALESCE(SUM(tokens_in+tokens_out),0), "
            "COALESCE(SUM(cost_usd),0) FROM agent_runs GROUP BY 1 ORDER BY 4 DESC").fetchall()
        by_day = conn.execute(
            "SELECT created_at::date d, COALESCE(SUM(cost_usd),0) FROM agent_runs "
            "WHERE created_at > current_date - 13 GROUP BY 1 ORDER BY 1").fetchall()
    today_pct = round(float(c["cost_today"]) / cap * 100) if cap else 0
    dmax = max([float(x[1]) for x in by_day], default=0) or 1
    bars = "".join(f'<i style="height:{max(3,round(float(v)/dmax*100))}%" title="{d} · ${float(v):.4f}"></i>'
                   for d, v in by_day) or '<i style=height:3%></i>'
    bal_card = ""
    if bal:
        bp = round(bal["used"] / bal["granted"] * 100) if bal.get("granted") else 0
        bal_card = (f'<div class=card><h3>OpenRouter balance</h3>'
                    f'<div class=stat><span class=unit>$</span>{bal["remaining"]:.2f}</div>'
                    f'<div class=meter><i style="width:{bp}%"></i></div>'
                    f'<div class=sub>${bal["used"]:.2f} used of ${bal["granted"]:.2f}</div></div>')
    model_rows = "".join(
        f'<div class=srow><div class=grow><div class=name>{html.escape(m)}</div>'
        f'<div class=meta>{calls} calls · {int(tok):,} tokens</div></div>'
        f'<div style="font-variant-numeric:tabular-nums;font-weight:600">${float(cost):.4f}</div></div>'
        for m, calls, tok, cost in by_model) or '<p class=sub>No spend yet.</p>'
    body = f"""
    <div class="grid g-3">
      <div class=card><h3>Spent today</h3><div class=stat><span class=unit>$</span><span data-cost-today>{c['cost_today']}</span></div>
        <div class=meter{' bad' if today_pct>90 else ' warn' if today_pct>75 else ''}><i style="width:{min(100,today_pct)}%"></i></div>
        <div class=sub>of ${cap:.2f} daily cap</div></div>
      <div class=card><h3>Total spent</h3><div class=stat><span class=unit>$</span>{c['cost_total']}</div>
        <div class=sub>{c['runs_total']} research runs</div></div>
      {bal_card or '<div class=card><h3>OpenRouter balance</h3><div class=sub style=margin-top:.6rem>balance check unavailable</div></div>'}
    </div>
    <div class=eyebrow>Spend · last 14 days</div>
    <div class=card><div class=bars>{bars}</div></div>
    <div class=eyebrow>By model</div>
    <div class="card pad-lg">{model_rows}</div>"""
    return shell("cost", "Cost", body)


@app.post("/api/kill")
def api_kill(_: bool = Depends(auth)):
    # stop the agent + any in-flight research; data is preserved; console stays up.
    _sh(["docker", "stop", "hermes"], timeout=30)
    _sh(["pkill", "-f", "pipeline.run"])
    return {"ok": True, "message": "Kill switch fired — Hermes stopped, in-flight research halted."}
