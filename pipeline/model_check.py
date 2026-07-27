"""Quick liveness check: confirm the OpenRouter key works and each locked model slug responds.
Usage: python -m pipeline.model_check   (reads OPENROUTER_* from env)
"""
from __future__ import annotations
import os
import requests

KEY = os.environ["OPENROUTER_API_KEY_ANALYST"]
MODELS = {
    "director (Hy3)":    os.environ.get("OPENROUTER_DIRECTOR_MODEL", "tencent/hy3-20260706"),
    "synth (MiniMax M3)": os.environ.get("OPENROUTER_SYNTH_MODEL", "minimax/minimax-m3-20260531"),
    "bulk (Nemotron free)": os.environ.get("OPENROUTER_BULK_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free"),
}


def check(label: str, slug: str) -> None:
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
            json={"model": slug, "max_tokens": 200,   # reasoning models need headroom past think tokens
                  "messages": [{"role": "user", "content": "reply with the single word: ok"}]},
            timeout=90,
        )
        if r.status_code != 200:
            print(f"  [FAIL] {label:22} {slug}\n         {r.status_code}: {r.text[:180]}")
            return
        body = r.json()
        msg = body["choices"][0]["message"]
        out = (msg.get("content") or msg.get("reasoning") or "")[:30]
        used = body.get("model", slug)
        cost = body.get("usage", {}).get("cost", 0)
        finish = body["choices"][0].get("finish_reason")
        print(f"  [OK]   {label:22} {slug}  -> '{out}'  (served: {used}, finish: {finish}, cost ${cost})")
    except Exception as e:
        print(f"  [ERR]  {label:22} {slug}: {type(e).__name__}: {e}")


if __name__ == "__main__":
    print("OpenRouter model liveness check:")
    for label, slug in MODELS.items():
        check(label, slug)
