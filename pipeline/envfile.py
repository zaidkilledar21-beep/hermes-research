"""Load the deployment's .env into os.environ for vars that are ABSENT (never overriding).

WHY THIS EXISTS: a process captures its environment at exec time, so editing .env does nothing
for anything already running. The web app is long-lived (uvicorn, restarted rarely) and
`/api/ask` spawns `pipeline.run` with `env=dict(os.environ)` — so every research run triggered
from Hermes chat inherited whatever environment uvicorn happened to start with. Measured: the web
app had been up since 2026-07-25 13:25, two days before the v3 flags were added, so runs 47-49
executed with PLANNER_ENABLED absent (defaults off) and reported `planner: fallback_disabled`
while .env on the same box said `PLANNER_ENABLED=1`. CLI-triggered runs were fine because the
operator sourced .env by hand, which is exactly the kind of difference that makes a bug look like
it is not there.

This is the same failure class as lesson 29's revoked-key incident: `docker restart` reuses the
captured environment, so the container kept authenticating with a rotated-away key. Restarting the
web app fixes today's symptom; loading the file at entry means the next flag added to .env does not
silently do nothing until somebody remembers to restart uvicorn.

SETDEFAULT SEMANTICS, deliberately: an explicitly-exported variable always wins over the file.
That keeps `PLANNER_ENABLED=0 python -m pipeline.run ...` working as an override for a single run,
and keeps the eval harness able to pin a model without editing the deployment's config.
"""
from __future__ import annotations
import os
import pathlib

# Default location matches deploy/: the repo checkout on the box is /home/trader/hermes-build,
# and .env sits at its root next to pipeline/.
DEFAULT_ENV_PATH = pathlib.Path(__file__).resolve().parent.parent / ".env"


def load(path: str | os.PathLike | None = None, *, override: bool = False) -> int:
    """Fill os.environ from a KEY=VALUE file. Returns how many vars were newly set.

    Parsing is deliberately minimal and matches what deploy/hermes-run.sh's envval() reads:
    KEY=VALUE per line, `#` comments, blank lines skipped, surrounding quotes stripped, and a
    trailing CR removed so a file edited on Windows cannot inject `\\r` into a secret (the exact
    corruption lesson 29 documents for the OpenRouter key).
    """
    p = pathlib.Path(path) if path is not None else DEFAULT_ENV_PATH
    if not p.is_file():
        return 0
    applied = 0
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    for raw in text.splitlines():
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key.startswith("export "):
            key = key.removeprefix("export ").strip()
        # -f2- equivalent: everything after the FIRST '=' is the value, since a value may
        # legitimately contain '=' (base64 padding, query strings).
        value = value.strip().rstrip("\r")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
            applied += 1
    return applied
