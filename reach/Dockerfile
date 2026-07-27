# Isolated walled-source scraper — Camoufox (stealth Firefox, Playwright-controlled).
# Holds ZERO real secrets; IG/FB sessions are burner storage_state files in a named volume.
# Reddit needs no login (public web renders anonymously in a real browser).
FROM python:3.12-slim

# Firefox runtime libs + a virtual display (xvfb) + VNC for the one-time interactive IG/FB login.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgtk-3-0 libx11-xcb1 libdbus-glib-1-2 libxt6 libasound2 libxtst6 libxrandr2 \
        libpangocairo-1.0-0 libpango-1.0-0 libcairo2 libgbm1 libxcomposite1 libxdamage1 \
        libxfixes3 libxcursor1 libgdk-pixbuf-2.0-0 fonts-liberation ca-certificates \
        xvfb x11vnc novnc websockify procps curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash reach \
    && mkdir -p /home/reach/state \
    && chown -R reach:reach /home/reach
USER reach
WORKDIR /home/reach
ENV PATH="/home/reach/.local/bin:${PATH}"

# Camoufox + its browser binary (fetched at build so runtime is offline-ready for the browser itself).
RUN pip install --user --no-cache-dir "camoufox[geoip]" playwright \
    && python -m camoufox fetch

COPY --chown=reach:reach reach_camoufox.py /home/reach/reach_camoufox.py
ENTRYPOINT ["python", "/home/reach/reach_camoufox.py"]
