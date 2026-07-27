#!/usr/bin/env bash
# Hermes Research Engine — KILL SWITCH.
# One command stops everything, preserves all evidence/state/logs. Run:  sudo bash hermes-kill.sh
# Reversible: `docker compose up -d` from /opt/hermes brings it back.

set -uo pipefail
echo "== KILL SWITCH =="

COMPOSE_DIR=/opt/hermes

echo "1. Stopping containers (hermes + reach)..."
if [ -f "$COMPOSE_DIR/docker-compose.yml" ]; then
  docker compose -f "$COMPOSE_DIR/docker-compose.yml" stop 2>/dev/null || true
else
  # fallback: stop by name if compose file not where expected
  docker stop hermes reach 2>/dev/null || true
fi

echo "2. Disabling scheduled jobs (research cron)..."
# research/collection cron lives in a dedicated crontab file; disable by moving it aside
if [ -f /etc/cron.d/hermes-research ]; then
  mv /etc/cron.d/hermes-research /etc/cron.d/hermes-research.disabled
  echo "   cron disabled."
else
  echo "   no active cron file."
fi

echo "3. Verifying nothing left listening on app ports..."
ss -tlnp 2>/dev/null | grep -E ':(8501|8787|11434|3000)\b' && echo "   ^ still listening (investigate)" || echo "   clean."

echo "== KILLED. Evidence/state/logs preserved under /opt/hermes and /opt/dropbox. =="
echo "Restore:  cd /opt/hermes && sudo docker compose up -d   (and re-enable cron if wanted)"
