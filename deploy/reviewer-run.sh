#!/usr/bin/env bash
# Launch the isolated reviewer container (Codex evidence-challenge + Claude quality-judgment).
# Holds ONLY the owner's CLI auth — no Neon/OpenRouter/platform secret. Default bridge network
# (needs internet for the CLIs; isolated from host services). --restart => survives reboot.
set -euo pipefail

REVIEW_DIR=/home/trader/review        # shared with the host pipeline (sanitized packets only)
AUTH_DIR=/home/trader/reviewer-auth   # holds the claude token file
mkdir -p "$REVIEW_DIR/req" "$REVIEW_DIR/out" "$AUTH_DIR"
# non-secret finding packets; host(trader) + container both write. Chmod is best-effort — a file
# already owned by the container's uid from a prior run can't be chmod'd by trader; don't die on it.
chmod -R 777 "$REVIEW_DIR" 2>/dev/null || find "$REVIEW_DIR" -writable -exec chmod 777 {} + 2>/dev/null || true

CLAUDE_TOKEN=""
[ -f "$AUTH_DIR/claude-token" ] && CLAUDE_TOKEN=$(cat "$AUTH_DIR/claude-token")

# Codex device-auth persists in a Docker named volume (Docker sets ownership to the container's
# user, avoiding the host-uid mismatch). Claude uses the OAuth token env — no mount needed.
docker rm -f reviewer 2>/dev/null || true
docker run -d --name reviewer --restart unless-stopped \
  --memory=1g \
  -v "$REVIEW_DIR":/app/review \
  -v reviewer_codex_auth:/home/reviewer/.codex \
  -e CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_TOKEN" \
  reviewer:latest

echo "reviewer container up."
[ -n "$CLAUDE_TOKEN" ] && echo "claude token: loaded" || echo "claude token: NOT set (put it in $AUTH_DIR/claude-token, then re-run)"
echo "next: docker exec -it reviewer codex login --device-auth   (approve the code on your phone)"
