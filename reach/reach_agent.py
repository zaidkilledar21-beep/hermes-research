"""Reach poller — the ONLY process in the isolated container.

Watches /app/dropbox/req for JSON request files, runs READ-ONLY agent-reach fetches for the
walled platforms (reddit/instagram/facebook), and writes raw results to /app/dropbox/out.
Hermes ingests out/ and tags everything UNTRUSTED_EVIDENCE. This process:
  - never runs any write/post/comment/dm agent-reach subcommand (allowlist below),
  - holds no real credential (only burner logins from burners.env),
  - treats request files as data (fixed subcommand shapes, no shell interpolation of req content).
"""
from __future__ import annotations
import json
import os
import pathlib
import subprocess
import time
import uuid

DROP = pathlib.Path("/app/dropbox")
REQ = DROP / "req"
OUT = DROP / "out"
POLL_SECONDS = 10

# Hard allowlist: platform -> the exact read subcommand agent-reach exposes. Nothing else runs.
READ_CMD = {
    "reddit_reach":    ["agent-reach", "reddit", "search"],
    "instagram_reach": ["agent-reach", "instagram", "read"],
    "facebook_reach":  ["agent-reach", "facebook", "read"],
}
# Any request naming a platform not in this map, or any verb other than these, is refused.


def handle(req_path: pathlib.Path) -> None:
    try:
        req = json.loads(req_path.read_text(encoding="utf-8"))
    except Exception as e:
        _fail(req_path.stem, f"unreadable request: {e}")
        req_path.unlink(missing_ok=True)
        return

    source = req.get("source")
    query = req.get("query", "")
    limit = int(req.get("limit", 25))
    base = READ_CMD.get(source)

    if not base:
        _fail(req.get("id", req_path.stem), f"refused: '{source}' not a read-allowlisted source")
        req_path.unlink(missing_ok=True)
        return

    # query passed as a discrete argv element (never interpolated into a shell string).
    cmd = base + ["--query", query, "--limit", str(min(limit, 50)), "--json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        results = proc.stdout if proc.returncode == 0 else ""
        err = proc.stderr if proc.returncode != 0 else None
    except subprocess.TimeoutExpired:
        results, err = "", "timeout"

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{req.get('id', req_path.stem)}.json").write_text(
        json.dumps({
            "id": req.get("id", req_path.stem),
            "run_id": req.get("run_id"),
            "source": source,
            "query": query,
            "raw": results,        # raw scraper output; hermes sanitizes + tags untrusted on ingest
            "error": err,
        }),
        encoding="utf-8",
    )
    req_path.unlink(missing_ok=True)


def _fail(rid: str, msg: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{rid}.json").write_text(
        json.dumps({"id": rid, "raw": "", "error": msg}), encoding="utf-8")


def main() -> None:
    REQ.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    print("[reach] poller up; read-only; sources:", ", ".join(READ_CMD))
    while True:
        for req_path in sorted(REQ.glob("*.json")):
            handle(req_path)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
