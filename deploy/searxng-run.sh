#!/usr/bin/env bash
# Launch the SearXNG web-search container — the engine's open-web search source.
# Loopback-only (host binds 127.0.0.1:8888); the pipeline's collect_websearch calls it.
# --restart unless-stopped => survives crashes/reboots (like reach/reviewer).
#
# The rendered settings.yml holds a secret (the residential proxy URL embeds its password), so it is
# written 0600, installed 0640 owned by the container's uid, and never world-readable. Only the
# template with __PROXY_URL__ / __REPLACE_AT_DEPLOY__ placeholders is committed (lessons.md #18: a
# real key reached git once already). Rendering is done by deploy/render-searxng-settings.py rather
# than sed — see that file for why argv and `set -a` were both leaking.
set -euo pipefail

CFG_DIR=/home/trader/searxng          # persistent config dir on the host (container-owned)
BUILD=/home/trader/hermes-build
REPO_SETTINGS="$BUILD/deploy/searxng/settings.yml"
RENDERER="$BUILD/deploy/render-searxng-settings.py"
SECRET_FILE=/home/trader/.searxng-secret
IMAGE=searxng/searxng:latest
mkdir -p "$CFG_DIR"

# Secret key lives OUTSIDE the container-owned config dir so a re-render never rotates it, and so
# this script never has to read back a file it cannot read. umask before creation: a chmod after
# the write leaves a window where the file is 0644.
if [ ! -f "$SECRET_FILE" ]; then
  ( umask 077; openssl rand -hex 32 > "$SECRET_FILE" )
  echo "searxng: generated secret_key"
fi
chmod 600 "$SECRET_FILE"   # repair permissions on a file created by an older version of this script

# Render fresh from the repo template every run — idempotent, and it removes the old
# "only copy if the repo file is newer" trap where an edited template silently never reached the
# container. The renderer reads .env itself and reads ONLY the proxy keys out of it.
STAGED=$(mktemp); chmod 600 "$STAGED"
trap 'rm -f "$STAGED"' EXIT
"$BUILD/.venv/bin/python" "$RENDERER" "$REPO_SETTINGS" "$BUILD/.env" "$SECRET_FILE" "$STAGED"

# The image's uid is read from the image, not hard-coded: settings are installed 0640, so if a
# future :latest changes its uid a hard-coded 977 would make the config unreadable and search would
# come up broken after an unrelated `docker pull`.
SEARXNG_UID=$(docker run --rm --entrypoint id "$IMAGE" -u 2>/dev/null || echo 977)
case "$SEARXNG_UID" in ''|*[!0-9]*) SEARXNG_UID=977 ;; esac

# Keep the currently-installed config so a bad render can be rolled back rather than left broken.
docker run --rm -v "$CFG_DIR":/cfg alpine:3 sh -c \
  'if [ -f /cfg/settings.yml ]; then cp /cfg/settings.yml /cfg/settings.yml.prev; fi'

install_settings() {  # install_settings <file-on-host>
  docker run --rm -v "$CFG_DIR":/cfg -v "$1":/new:ro alpine:3 sh -c \
    "cp /new /cfg/settings.yml && chown ${SEARXNG_UID}:${SEARXNG_UID} /cfg/settings.yml \
     && chmod 640 /cfg/settings.yml"
}
install_settings "$STAGED"

start_container() {
  docker rm -f searxng >/dev/null 2>&1 || true
  docker run -d --name searxng --restart unless-stopped \
    --memory=400m \
    -p 127.0.0.1:8888:8080 \
    -v "$CFG_DIR":/etc/searxng \
    "$IMAGE" >/dev/null
}
start_container

# Prove it actually SERVES before claiming success. `docker run -d` only says a container was
# created — a settings file SearXNG cannot parse leaves it restart-looping while the old, working
# container is already gone.
healthy() {
  for _ in $(seq 1 20); do
    sleep 3
    if curl -sf --max-time 10 "http://127.0.0.1:8888/search?q=ping&format=json" \
         | grep -q '"results"'; then return 0; fi
  done
  return 1
}

if healthy; then
  echo "searxng up → 127.0.0.1:8888 (JSON: /search?q=<q>&format=json)"
else
  echo "searxng: NEW CONFIG FAILED TO SERVE — rolling back to the previous settings" >&2
  docker logs searxng --tail 20 >&2 || true
  if docker run --rm -v "$CFG_DIR":/cfg alpine:3 test -f /cfg/settings.yml.prev; then
    docker run --rm -v "$CFG_DIR":/cfg alpine:3 sh -c \
      "cp /cfg/settings.yml.prev /cfg/settings.yml && chown ${SEARXNG_UID}:${SEARXNG_UID} /cfg/settings.yml"
    start_container
    healthy && echo "searxng: rolled back and serving" >&2
  fi
  exit 1
fi
