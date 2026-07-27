# Isolated reviewer container. Holds ONLY the owner's Claude Code + Codex CLI auth.
# No Neon / OpenRouter / platform secret ever mounted here. Both reviewers run tool-less + one-shot.
FROM node:22-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# Subscription-backed CLIs (owner authenticates them once via device-auth after deploy).
RUN npm install -g @anthropic-ai/claude-code @openai/codex

RUN useradd --create-home --shell /bin/bash reviewer \
    && mkdir -p /home/reviewer/.codex \
    && chown -R reviewer:reviewer /home/reviewer/.codex
# A fresh named volume mounted at .codex inherits this dir's ownership (reviewer), so codex can write.
USER reviewer
WORKDIR /home/reviewer
# ~/.claude and ~/.codex live under $HOME; mount a volume there so device-auth persists across restarts.

COPY --chown=reviewer:reviewer reviewer_agent.py /home/reviewer/reviewer_agent.py

ENTRYPOINT ["python3", "/home/reviewer/reviewer_agent.py"]
