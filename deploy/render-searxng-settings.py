#!/usr/bin/env python3
"""Render the SearXNG settings file, keeping the proxy password out of every process argv.

  render-searxng-settings.py <template> <env-file> <secret-file> <output>

WHY THIS IS NOT A `sed` ONE-LINER, which is what it replaced:
  - `sed "s|__PROXY_URL__|$PROXY_URL|"` puts the credentialed URL in the command line, and
    /proc/<pid>/cmdline is world-readable — the password was visible to any local process for the
    duration of the call, and to anything sampling argv (audit daemons, `ps` loops).
  - `set -a; . ./.env; set +a` exported EVERY secret in .env (DATABASE_URL, OpenRouter keys, the X
    bearer token) into the environment of every later command in the script, including `docker run`.
    Here only the four REACH_PROXY_* / SEARXNG_* keys are read, and nothing is exported at all.
  - A `sed` replacement treats `&` and `\\` as syntax, so a password containing either rendered a
    corrupted proxy URL — silently, since SearXNG would just fail to authenticate.

Paths are the only arguments. Output is written 0600; the caller installs it.
"""
from __future__ import annotations

import os
import sys

WANTED = ("REACH_PROXY_SERVER", "REACH_PROXY_USER", "REACH_PROXY_PASS", "REACH_PROXY_COUNTRY",
          "SEARXNG_PROXY_COUNTRY")


def read_env(path: str) -> dict[str, str]:
    """Read ONLY the keys this renderer needs. Deliberately not a shell source: a .env holds the
    database URL and every model key, and none of that belongs in this process."""
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key in WANTED:
                values[key] = value.strip().strip('"').strip("'")
    return values


def proxy_url(env: dict[str, str]) -> str | None:
    """http://user__cr.<country>:pass@host:port, or None when no usable proxy is configured.

    The password is checked, not just the username: an empty password renders a URL that
    authenticates against nothing, and the old script would have installed it over a working
    container and called the deploy a success.
    """
    server = env.get("REACH_PROXY_SERVER", "")
    user = env.get("REACH_PROXY_USER", "")
    password = env.get("REACH_PROXY_PASS", "")
    if not server or not user or not password:
        return None
    host = server.split("://", 1)[-1].rstrip("/")
    # Search gets its own exit country: reach pins one country to keep the Instagram burner session
    # consistent, but that pin would localize every search result away from the market in question.
    country = env.get("SEARXNG_PROXY_COUNTRY") or env.get("REACH_PROXY_COUNTRY") or ""
    if country:
        user = f"{user}__cr.{country}"
    from urllib.parse import quote
    return f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}"


def main() -> int:
    template, env_file, secret_file, output = sys.argv[1:5]
    text = open(template, encoding="utf-8").read()
    text = text.replace("__REPLACE_AT_DEPLOY__", open(secret_file, encoding="utf-8").read().strip())

    env = read_env(env_file)
    url = proxy_url(env)
    if url:
        # Exactly once. The template's own comment used to spell the placeholder out, so a plain
        # replace wrote the live credential into the comment as well — the password appeared twice
        # and the "any placeholder left?" check below could never fire, because the comment was
        # always substituted too.
        occurrences = text.count("__PROXY_URL__")
        if occurrences != 1:
            print(f"searxng: ERROR — expected exactly one proxy placeholder, found {occurrences}",
                  file=sys.stderr)
            return 1
        text = text.replace("__PROXY_URL__", url)
        country = env.get("SEARXNG_PROXY_COUNTRY") or env.get("REACH_PROXY_COUNTRY") or "default"
        print(f"searxng: outgoing proxy configured (exit {country})")
    else:
        # Drop the proxies block entirely rather than leave a placeholder SearXNG would dial as a
        # literal hostname.
        kept, skipping = [], False
        for line in text.splitlines(keepends=True):
            if line.startswith("  proxies:"):
                skipping = True
                continue
            if skipping:
                if line.strip().startswith(("-", "all://")):
                    continue
                skipping = False
            kept.append(line)
        text = "".join(kept)
        print("searxng: no usable REACH_PROXY_* — running direct "
              "(datacenter IP, expect engine suspensions)", file=sys.stderr)

    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    if "__PROXY_URL__" in text or "__REPLACE_AT_DEPLOY__" in text:
        print("searxng: ERROR — placeholder left unrendered", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
