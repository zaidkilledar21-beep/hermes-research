#!/usr/bin/env bash
# Bring up the Hermes gateway + dashboard (Stage 1: chat with the Hy3 agent).
# Dashboard bound to host loopback (127.0.0.1:9119), basic-auth'd, fronted by Cloudflare tunnel.
# --restart unless-stopped => Docker relaunches it on reboot (docker.service is enabled).
set -euo pipefail

# Read one KEY=VALUE out of an env file. `-f2-` (not `-f2`) because a value may legitimately
# contain '=' — base64 padding, a query string — and truncating there hands the container a
# silently corrupt secret that only shows up as a provider-side auth error. `tr -d '\r'` because
# a file written or edited from Windows carries CRLF, and a trailing CR corrupts an HTTP header
# just as invisibly. See lessons.md #29.
envval() { grep -m1 "^$1=" "$2" | cut -d= -f2- | tr -d '\r'; }

# Research-engine API creds (single source of truth = the web app's .env) so the run-research
# skill can reach http://localhost:8080 from inside the container (host networking).
WEBENV=/home/trader/hermes-build/.env
RESEARCH_API_USER=$(envval HERMES_WEB_USER "$WEBENV")
RESEARCH_API_PASS=$(envval HERMES_WEB_PASS "$WEBENV")
# OpenRouter is a first-class Hermes provider; it reads OPENROUTER_API_KEY from the environment.
OPENROUTER_KEY=$(envval OPENROUTER_API_KEY_ANALYST "$WEBENV")

# Dashboard basic-auth: set DASH_BASIC=1 to enable Hermes's own (buggy) basic login. Default OFF —
# gating is done at the edge by Cloudflare Access, which dodges the root-URL 302→500 bug.
# dash.env lives in hermes-build (trader-owned); ~/.hermes is owned by the container user.
DASH_AUTH_ARGS=()
if [ "${DASH_BASIC:-0}" = "1" ]; then
  DASH_ENV=/home/trader/hermes-build/.hermes-dash.env
  if [ ! -f "$DASH_ENV" ]; then
    echo "HERMES_DASH_USER=zaid" > "$DASH_ENV"
    echo "HERMES_DASH_PASS=$(openssl rand -base64 12 | tr -dc 'A-Za-z0-9' | head -c 14)" >> "$DASH_ENV"
    chmod 600 "$DASH_ENV"
  fi
  set -a; . "$DASH_ENV"; set +a
  DASH_AUTH_ARGS=(-e HERMES_DASHBOARD_BASIC_AUTH_USERNAME="$HERMES_DASH_USER"
                  -e HERMES_DASHBOARD_BASIC_AUTH_PASSWORD="$HERMES_DASH_PASS")
  echo "dashboard basic-auth ON — user=$HERMES_DASH_USER pass=$HERMES_DASH_PASS"
fi

# Host networking: the container shares the host net stack, so the skill reaches the web app at
# localhost:8080 (loopback, no ufw/bridge issue). Dashboard binds host loopback:9119 for cloudflared;
# never on a public interface. Hermes is the trusted orchestrator; the isolated reach container is NOT
# host-networked. ufw still blocks all external inbound except SSH.
docker rm -f hermes 2>/dev/null || true
docker run -d --name hermes --restart unless-stopped \
  --network host \
  -v /home/trader/.hermes:/opt/data \
  --memory=2g \
  -e HERMES_DASHBOARD=1 \
  -e HERMES_DASHBOARD_HOST=127.0.0.1 \
  -e HERMES_DASHBOARD_PORT=9119 \
  -e HERMES_DASHBOARD_PUBLIC_URL="https://chat.example.com" \
  -e RESEARCH_API_USER="$RESEARCH_API_USER" \
  -e RESEARCH_API_PASS="$RESEARCH_API_PASS" \
  -e OPENROUTER_API_KEY="$OPENROUTER_KEY" \
  "${DASH_AUTH_ARGS[@]}" \
  nousresearch/hermes-agent:v2026.7.7 gateway run

# Point Hermes at its first-class OpenRouter provider (reads OPENROUTER_API_KEY from env above).
sleep 5
docker exec hermes hermes config set model.provider openrouter >/dev/null 2>&1 || true
docker exec hermes hermes config set model.default tencent/hy3-20260706 >/dev/null 2>&1 || true
docker restart hermes >/dev/null 2>&1 || true

if [ "${DASH_BASIC:-0}" = "1" ]; then
  echo "hermes up → 127.0.0.1:9119 · dashboard user=$HERMES_DASH_USER pass=$HERMES_DASH_PASS"
else
  echo "hermes up → 127.0.0.1:9119 · gated by Cloudflare Access only (no local dashboard password)"
fi
