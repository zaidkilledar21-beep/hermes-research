#!/usr/bin/env bash
# Launch the isolated reach container (walled-source scraper: Reddit/Instagram/Facebook).
# Holds ZERO real credentials — burner platform logins only, seeded interactively via `docker exec`.
set -euo pipefail

DROPBOX_DIR=/home/trader/dropbox   # shared with the host pipeline (scraped text only, no secrets)
mkdir -p "$DROPBOX_DIR/req" "$DROPBOX_DIR/out"

# Residential proxy (required to pass platforms' datacenter-IP blocks). Read from the web .env if set.
WEBENV=/home/trader/hermes-build/.env
PROXY_ARGS=()
if grep -q '^REACH_PROXY_SERVER=' "$WEBENV" 2>/dev/null; then
  PROXY_ARGS=(-e "REACH_PROXY_SERVER=$(grep '^REACH_PROXY_SERVER=' "$WEBENV" | cut -d= -f2-)"
              -e "REACH_PROXY_USER=$(grep '^REACH_PROXY_USER=' "$WEBENV" | cut -d= -f2-)"
              -e "REACH_PROXY_PASS=$(grep '^REACH_PROXY_PASS=' "$WEBENV" | cut -d= -f2-)"
              -e "REACH_PROXY_COUNTRY=$(grep '^REACH_PROXY_COUNTRY=' "$WEBENV" | cut -d= -f2-)")
  echo "residential proxy: configured"
else
  echo "residential proxy: NOT set — Reddit/IG/FB will hit datacenter-IP blocks until REACH_PROXY_* is in .env"
fi
chmod -R 777 "$DROPBOX_DIR" 2>/dev/null || find "$DROPBOX_DIR" -writable -exec chmod 777 {} + 2>/dev/null || true

docker rm -f reach 2>/dev/null || true
docker run -d --name reach --restart unless-stopped \
  --memory=1.5g -p 127.0.0.1:5900:5900 -p 127.0.0.1:6080:6080 \
  -v "$DROPBOX_DIR":/app/dropbox \
  -v reach_state:/home/reach/state \
  "${PROXY_ARGS[@]}" \
  reach:latest

echo "reach (camoufox) up."
echo "  Reddit  → works headless now, NO login needed (public web)."
echo "  IG / FB → one-time burner login:  docker exec -it reach python reach_camoufox.py login <instagram|facebook>"
