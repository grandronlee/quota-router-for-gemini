# quota-router-for-gemini (DESIGN §21.1). Two stages: `runtime` (default) and `test`.
# The base image is pinned by digest; Dependabot proposes updates (.github/dependabot.yml).
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt ./
RUN pip install --require-hashes --no-deps -r requirements.txt

# Owner-only state folder. A new named volume copies this owner and mode on first mount,
# so data/ doesn't depend on the umask (DESIGN §21.1).
RUN mkdir -m 0700 /app/data && chown app:app /app/data

COPY quota_router/ quota_router/
COPY scripts/ scripts/
RUN python -m compileall -q quota_router scripts

# ---------------------------------------------------------------------------
FROM base AS test
COPY requirements-dev.txt ./
RUN pip install --require-hashes --no-deps -r requirements-dev.txt
COPY pyproject.toml config.example.yaml ./
COPY tests/ tests/
USER app
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

# ---------------------------------------------------------------------------
FROM base AS runtime
USER app
EXPOSE 4000
# Listens on all interfaces inside the container only; compose publishes 127.0.0.1:4000.
ENV PROXY_HOST=0.0.0.0 \
    PROXY_PORT=4000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:4000/healthz', timeout=4).status == 200 else 1)"]
CMD ["python", "-m", "quota_router"]
