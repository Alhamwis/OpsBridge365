# OpsBridge365 — cloud layer

Microsoft Graph → SharePoint sync job and a live service-desk SLA metrics API,
packaged as one container image with two entrypoints.

| Entrypoint | Command | Runs as |
| --- | --- | --- |
| Metrics API | `uvicorn app.main:app --host 0.0.0.0 --port 8000` (default `CMD`) | Azure Container App, HTTP-triggered, scale-to-zero |
| Sync job | `python -m app.sync` | Azure Container Apps Job, cron trigger, run-and-exit |

## Endpoints

- `GET /healthz` — liveness probe. Always answers `200 {"status":"ok","version":"..."}`,
  including before any Azure credential is mounted. A health probe that needs
  secrets would have the orchestrator kill containers that are merely unconfigured.
- `GET /metrics` — live open / at-risk / SLA-compliance numbers from the Tickets
  list. This is the endpoint that requires configuration; without it, `503`.

No response ever echoes configuration or secrets — failures are logged in full
and returned as generic messages.

## Configuration

Environment variables only; see `.env.example` for the full list. In Azure these
come from Container Apps secrets backed by Key Vault, never from a file baked
into the image.

## Local development

```bash
pip install -e ".[dev]"
pytest
```

## Container

```bash
docker build -t opsbridge365:local .
docker run --rm -p 8000:8000 opsbridge365:local            # API
docker run --rm --env-file .env opsbridge365:local python -m app.sync   # sync job
```

The image runs as non-root (uid 10001) and contains no credentials. Build and
run evidence: [`evidence/docker/build-and-run.md`](evidence/docker/build-and-run.md).
