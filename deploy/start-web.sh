#!/usr/bin/env bash
# Persistent launcher for the research web UI (loopback; Cloudflare Tunnel fronts it).
# Invoked at boot via the trader user's crontab (@reboot) — no sudo, no systemd needed.
cd /home/trader/hermes-build || exit 1
set -a; . ./.env; set +a
exec ./.venv/bin/uvicorn web.app:app --host 127.0.0.1 --port 8080 --log-level info
