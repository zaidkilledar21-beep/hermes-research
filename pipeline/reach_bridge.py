"""Hermes-side bridge to the isolated reach container, over the shared dropbox volume.

request_reach()  — drop a scrape request for reddit/instagram/facebook into req/.
ingest_reach()   — read reach's result files from out/, store as UNTRUSTED_EVIDENCE, clear them.

Runs on the hermes side (has DATABASE_URL). The reach container never touches Neon.
"""
from __future__ import annotations
import argparse
import json
import os
import pathlib
import sys
import time
import uuid
from collectors import common

# Pipeline runs on the host; the reach container shares this dir. Override with DROPBOX_DIR.
DROP = pathlib.Path(os.environ.get("DROPBOX_DIR", "/opt/dropbox"))
REQ = DROP / "req"
OUT = DROP / "out"

VALID = {"reddit_reach", "instagram_reach", "facebook_reach",
         "stackexchange_reach", "trustpilot_reach", "forum_reach", "reddit_threads"}


def request_reach(run_id: int, source: str, query: str, limit: int = 25,
                  urls: list[str] | None = None) -> str:
    """Drop a scrape request for the reach container.

    `urls` is for thread-reading sources (reddit_threads): the whole batch goes in ONE request so
    the container reads them all in a single browser session — launching a fresh Camoufox per
    thread is the main memory risk on a 4GB box.
    """
    if source not in VALID:
        raise ValueError(f"{source} is not a walled/reach source")
    REQ.mkdir(parents=True, exist_ok=True)
    rid = uuid.uuid4().hex[:12]
    req = {"id": rid, "run_id": run_id, "source": source, "query": query, "limit": limit}
    if urls:
        req["urls"] = list(urls)
    (REQ / f"{rid}.json").write_text(json.dumps(req), encoding="utf-8")
    return rid


def ingest_reach_detailed(run_id: int) -> tuple[int, set[str]]:
    """Ingest ready results and report WHICH request ids completed.

    Callers need the ids because a fixed sleep is a race: reading several browser-rendered threads
    takes minutes, and anything the container writes after the caller stops polling is orphaned in
    the dropbox forever (this silently lost an entire community-evidence batch on run 27)."""
    stored = ingest_reach(run_id, _completed := set())
    return stored, _completed


def ingest_reach(run_id: int, completed: set[str] | None = None) -> int:
    """Pull all ready results for this run into evidence. Returns count stored."""
    OUT.mkdir(parents=True, exist_ok=True)
    stored = 0
    for f in sorted(OUT.glob("*.json")):
        try:
            res = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            f.unlink(missing_ok=True)
            continue
        if res.get("run_id") != run_id:
            continue
        rid = res.get("id") or f.stem
        if res.get("error"):
            print(f"[reach:{res.get('source')}] error: {res['error']}", file=sys.stderr)
            if completed is not None:
                completed.add(rid)  # errored, but this request IS finished — don't wait on it
            f.unlink(missing_ok=True)
            continue
        # reach returns a JSON blob of items in `raw`; store each as untrusted evidence.
        raw = res.get("raw", "")
        n_before = stored
        for url, text, meta in _split_items(raw):
            # store_evidence auto-tags UNTRUSTED_EVIDENCE because source_id is in WALLED_SOURCES.
            try:
                if common.store_evidence(run_id, res["source"], url, text, meta=meta):
                    stored += 1
            except Exception as e:  # one malformed record must not abort the whole batch
                print(f"[reach:{res.get('source')}] store failed: {type(e).__name__}: {e}",
                      file=sys.stderr)
        print(f"[reach:{res.get('source')}] ingested {stored - n_before} items (req {rid})",
              file=sys.stderr)
        if completed is not None:
            completed.add(rid)
        f.unlink(missing_ok=True)
    return stored


# Per-item provenance a community reader may return. Carried through to evidence_items so
# corroboration can be counted by DISTINCT author/thread instead of raw item count.
_META_KEYS = ("author", "thread_id", "thread_title", "comment_id", "parent_id",
              "depth", "page_ownership")
# Reader key -> evidence_items column, where the names deliberately differ.
_META_RENAMES = {"kind": "item_kind", "score": "item_score", "sort": "sort_mode"}


def _split_items(raw: str) -> list[tuple[str | None, str, dict]]:
    """Parse reach output into (url, text, meta) triples. Readers that return only url/text still
    work — meta simply comes back empty."""
    try:
        data = json.loads(raw)
    except Exception:
        return [(None, raw, {})] if raw.strip() else []
    if isinstance(data, dict):
        data = data.get("results") or data.get("items") or [data]
    out = []
    for it in data if isinstance(data, list) else []:
        if isinstance(it, dict):
            text = it.get("text") or it.get("body") or it.get("content") or json.dumps(it)
            meta = {k: it[k] for k in _META_KEYS if it.get(k) is not None}
            for src_key, col in _META_RENAMES.items():
                if it.get(src_key) is not None:
                    meta[col] = it[src_key]
            if not isinstance(meta.get("item_score"), int):
                meta.pop("item_score", None)  # column is INT; drop unparsed scores
            out.append((it.get("url") or it.get("permalink"), text, meta))
        else:
            out.append((None, str(it), {}))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    rq = sub.add_parser("request"); rq.add_argument("--run", type=int, required=True)
    rq.add_argument("--source", required=True); rq.add_argument("--query", required=True)
    rq.add_argument("--limit", type=int, default=25)
    ig = sub.add_parser("ingest"); ig.add_argument("--run", type=int, required=True)
    ig.add_argument("--wait", type=int, default=0, help="seconds to wait for results before ingesting")
    a = ap.parse_args()
    if a.cmd == "request":
        print(request_reach(a.run, a.source, a.query, a.limit))
    else:
        if a.wait:
            time.sleep(a.wait)
        print(f"ingested {ingest_reach(a.run)} reach items for run {a.run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
