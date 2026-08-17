# syntax=docker/dockerfile:1
#
# One image, two entrypoints:
#   API (default) : uvicorn app.main:app --host 0.0.0.0 --port 8000
#   Sync job      : python -m app.sync      (override the command; same image)
#
# Multi-stage so the compiler toolchain stays in the builder and never ships.

# ---------------------------------------------------------------- builder ----
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# Build deps for any dependency without a cp312 manylinux wheel. Builder only.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /src
# README.md is referenced by pyproject's `readme` field, so the build needs it.
COPY pyproject.toml README.md ./
COPY app ./app

# Runtime dependencies only. The [dev] extra (pytest, respx, ruff) is
# deliberately NOT installed - test tooling has no place in a runtime image.
RUN pip install .

# ---------------------------------------------------------------- runtime ----
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Fixed uid/gid so volume ownership and Azure pod security policy are predictable.
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin appuser

COPY --from=builder --chown=root:root /opt/venv /opt/venv

WORKDIR /srv
COPY --chown=root:root app ./app

# Nothing in the image is writable by the runtime user; no credentials are baked
# in. Configuration arrives as environment variables at run time.
USER 10001:10001

EXPOSE 8000

# urllib from the stdlib - adding curl just for a probe would grow the image
# and the attack surface for no gain.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
