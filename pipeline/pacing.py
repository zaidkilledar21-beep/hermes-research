"""Cross-process request pacing — one shared clock for a rate limit that is shared.

Research runs execute as SEPARATE PROCESSES (two at a time during a campaign), and the limits that
actually bite are per-account, not per-process:

  - OpenRouter's free tier allows 20 requests/minute across everything using the key. The bulk
    extraction stage ran 8 concurrent workers with no client-side limit, so a 268-item run buried
    the limit instantly and nearly every item came back 429 — which the caller saw as
    `KeyError: 'choices'` and recorded as a failed extraction. Extraction is load-bearing, so the
    whole run silently degraded to raw, un-cleaned evidence with no relevance verdicts.
  - SearXNG's upstream engines suspend the whole exit IP, so two runs pacing themselves
    independently still present double the rate to the thing doing the suspending.

An in-process lock cannot see either. The pace mark is therefore a file mtime guarded by an
exclusive flock: whoever holds the lock sleeps out the remainder of the interval and re-stamps it,
so N processes interleave at the shared interval instead of each keeping its own.

Fail-open by design: if the lock file cannot be used, pacing degrades to a plain local sleep rather
than blocking work. A missed pace costs a retry; a hang costs the run.
"""
from __future__ import annotations

import os
import threading
import time

_local_lock = threading.Lock()


def pace(path: str, interval: float) -> None:
    """Block until at least `interval` seconds have passed since any process last called this."""
    if interval <= 0:
        return
    with _local_lock:
        try:
            import fcntl

            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                wait = interval - (time.time() - os.fstat(fd).st_mtime)
                if wait > 0:
                    time.sleep(min(wait, interval))
                os.utime(fd, None)
            finally:
                os.close(fd)
        except Exception:
            time.sleep(interval)


def interval_for_rpm(requests_per_minute: float) -> float:
    """Seconds between requests for a per-minute quota. Returns 0 when unlimited."""
    return 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0
