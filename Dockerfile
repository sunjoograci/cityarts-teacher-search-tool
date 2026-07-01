# Playwright's official image ships Chromium + all required system libraries,
# so we don't need `--with-deps` (which fails on Render because it needs root).
# The image tag MUST match the playwright version pinned in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# git is used at runtime by scrape_service.py to commit and push the updated
# database back to GitHub after a scrape.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium is preinstalled in the base image; this is a cheap no-op that
# guarantees the browser matching the installed playwright build is present.
RUN playwright install chromium

# Copy the whole repo, including .git — the publish step runs git commands
# from this directory and pushes HEAD to the main branch.
COPY . .

# Render provides $PORT at runtime; shell form lets it expand.
CMD gunicorn scrape_service:app --bind 0.0.0.0:$PORT --timeout 600
