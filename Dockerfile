# syntax=docker/dockerfile:1
#
# One image, two entrypoints:
#   API (default) : uvicorn app.main:app --host 0.0.0.0 --port 8000
#   Sync job      : python -m app.sync      (override the command; same image)
#
# Multi-stage so the compiler toolchain stays in the builder and never ships.
#
# Reproducibility
# ---------------
# Two things decide whether two builds of the same commit produce the same
# image, and both are pinned here:
#
#   1. The base image, by DIGEST. `python:3.12-slim` is a moving tag - it
#      advanced on 2026-08-27 - so a tag-only build is a different image
#      tomorrow with no commit to point at. The version comment stays for
#      humans; the digest is what Docker actually resolves.
#   2. The dependency set, by HASH. requirements.txt is a fully resolved,
#      hash-pinned lock generated from pyproject.toml (see docs/DEPLOYMENT.md
#      for the regeneration command). `pip install .` would re-resolve every
#      `>=` floor at build time, so the image could pick up a release that no
#      test in this repository has ever run against.

# ---------------------------------------------------------------- builder ----
FROM python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217 AS builder

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

# Dependencies first, from the lock, in their own layer: application code
# changes far more often than the dependency set, so this layer stays cached
# across ordinary commits.
COPY requirements.txt ./
# --require-hashes makes pip refuse anything whose artifact hash is not listed.
# A tampered or substituted wheel fails the build instead of shipping.
RUN pip install --require-hashes --no-deps -r requirements.txt

# README.md is referenced by pyproject's `readme` field, so the build needs it.
COPY pyproject.toml README.md ./
COPY app ./app

# The project itself, with dependency resolution switched off - everything it
# needs is already installed above, at the pinned versions. The [dev] extra
# (pytest, respx, ruff) is deliberately never installed: test tooling has no
# place in a runtime image.
RUN pip install --no-deps .

# ---------------------------------------------------------------- runtime ----
FROM python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217 AS runtime

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
# and the attack surface for no gain. /healthz is public and needs no token,
# which is why the probe still works after /metrics was closed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
