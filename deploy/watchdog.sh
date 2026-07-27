#!/usr/bin/env bash
# Lightweight process watchdog — no sudo, no systemd (matches this box's whole deploy style).
# Cron runs this every minute; each block starts its process ONLY if not already running, so
# it's safe to run repeatedly / concurrently with a manual restart. Covers the 3 bare host
# processes that have no other supervision — the hermes/reach/reviewer containers already
# self-restart via `docker run --restart unless-stopped`, so they're not duplicated here.
set -u
LOG=/tmp/watchdog.log
ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

# 1. Web app (research console + /api/ask + /api/run — what Hermes' skill calls).
if ! pgrep -f "uvicorn web.app:app" >/dev/null; then
  echo "$(ts) [watchdog] web app down - restarting" >> "$LOG"
  cd /home/trader/hermes-build && set -a; . ./.env; set +a
  setsid nohup ./.venv/bin/uvicorn web.app:app --host 127.0.0.1 --port 8080 --log-level info \
    >/tmp/web.log 2>&1 < /dev/null &
  disown
fi

# 2. Caddy (loopback shim the hermes dashboard websocket needs — see lessons.md #10).
if ! pgrep -f "caddy run" >/dev/null; then
  echo "$(ts) [watchdog] caddy down - restarting" >> "$LOG"
  setsid nohup /home/trader/bin/caddy run --config /home/trader/hermes-build/Caddyfile \
    >/tmp/caddy.log 2>&1 < /dev/null &
  disown
fi

# 3. Cloudflare Tunnel (research.example.com + chat.example.com). --no-autoupdate: cloudflared
#    silently self-updates and exits (that's what took the tunnel down on 2026-07-24 — see
#    lessons.md); this box had no restart-on-exit supervisor for that handoff, so autoupdate is
#    the wrong default here — we control the binary version deliberately instead.
if ! pgrep -f "tunnel run research" >/dev/null; then
  echo "$(ts) [watchdog] cloudflared down - restarting" >> "$LOG"
  setsid nohup /home/trader/bin/cloudflared --no-autoupdate tunnel run research \
    >/tmp/cf-tunnel.log 2>&1 < /dev/null &
  disown
fi
