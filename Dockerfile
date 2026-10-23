# Persistent worker + Streamlit dashboard for ATS Resume Tailor / Auto Apply.
# Build:  docker compose build      Run: docker compose up -d
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HUNTER_DB=/app/data/hunter.sqlite3

WORKDIR /app

# DejaVu fonts are what ats/exporting.py uses for PDF rendering on Linux.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core tini \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt \
 && python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* \
 && chmod -R a+rX /ms-playwright

COPY . .

# Unprivileged runtime user; only /app/data (a volume) is writable.
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /app/data \
 && chown -R app:app /app/data
USER app

VOLUME ["/app/data"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "autoapply.worker"]
