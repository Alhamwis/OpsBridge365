# OpsBridge365 — cloud layer

OpsBridge365 keeps an IT service desk's device inventory honest: a scheduled job
pulls users and devices from Microsoft Graph and writes them into a SharePoint
**Assets** list, and a small HTTP API reads the **Tickets** list and returns live
SLA numbers. It is packaged as one container image with two entrypoints, deployed
by one Bicep template, and designed to cost nothing while idle.

> **Deployment status, stated up front.** The accounts are real and the data plane
> is live; the Azure compute is not deployed. Concretely: the repository is public,
> its pipeline runs, and it publishes a container image to ghcr.io. The Microsoft
> 365 tenant, the `opsbridge-graph` app registration with three admin-consented
> application permissions, the SharePoint site and both lists all exist — twelve
> integration tests pass against them. What does **not** exist is anything
> `infra/main.bicep` declares: no Log Analytics workspace, no Container Apps
> environment, no Key Vault, no sync Job, no API. The deploy job has run once and
> failed on an OIDC subject mismatch; the federated credential has been corrected
> and the job has not been re-run since. The [status table](#status) puts every
> component in one of three buckets — live and verified, built but not deployed,
> not started — and nothing is promoted a bucket for looking good.

---

## The problem it solves

A service desk accumulates two recurring, boring, error-prone chores:

1. **Asset reconciliation.** The device inventory in SharePoint drifts from what
   the directory actually knows. Who is a laptop assigned to? Is it compliant?
   When did it last check in? Someone updates the list by hand, or nobody does.
2. **SLA visibility.** "How many tickets are open, how many are about to breach,
   and did we hit our targets last week?" is answered by opening a list and
   counting, or by a report that nobody runs.

OpsBridge365 automates both. The sync job reconciles Graph device data into the
Assets list on a cron. The API answers the SLA question over HTTP in one call.

Two deliberate honesty rules run through the whole thing, because a wrong value in
an asset register is worse than an admitted gap:

- A device matches an Assets row by **serial number first, then device name**. A
  key matching more than one row is ambiguous, so it matches **nothing**.
- When a value cannot be determined, the literal string `"Unknown"` is written —
  never a guess. An unknown `LastCheckIn` is a date column, so it is left
  untouched rather than stamped with an invented time, and the count of those
  appears in the job's JSON summary.

The same rule governs the metrics: `sla_compliance_7d_pct` returns `null` when
nothing was resolved in the window, rather than a misleading 0% or 100%.

---

## Architecture

```mermaid
flowchart LR
    dev["git push to main"] --> gha["GitHub Actions"]
    gha -->|"build and push"| ghcr["GHCR - published image"]
    gha -->|"OIDC federated login"| arm["az deployment group create"]
    arm --> bicep["infra/main.bicep"]

    ghcr --> job["Container Apps Job - opsbridge-sync<br/>cron, run-and-exit"]
    ghcr --> api["Container App - opsbridge-api<br/>HTTP, scale-to-zero"]

    kv["Key Vault - graph client secret"] -->|"managed identity"| job
    kv -->|"managed identity"| api

    job --> msgraph["Microsoft Graph"]
    api --> msgraph
    msgraph --> users["users and devices"]
    msgraph --> sp["SharePoint lists"]

    job -->|"PATCH"| sp
    api -->|"read"| sp

    job --> logs["Log Analytics"]
    api --> logs
```

One image, two entrypoints. The API runs the image's default `CMD` (uvicorn); the
job overrides it with `python -m app.sync`. One build, one push, two workloads.

Full component and data-flow detail, and the reasoning behind each choice, is in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Quickstart

### Run the tests

```bash
pip install -e ".[dev]"
python -m pytest -q
```

Every test in the default run is offline — `httpx` is intercepted by `respx` and
MSAL is replaced by a stub, so no token request and no Graph call leaves the
machine. No credentials needed.

#### Live integration tests (opt-in)

`tests/integration/` holds twelve tests that hit the real Microsoft 365 tenant.
**All twelve pass** against it — `12 passed, 58 deselected in 10.06s`, captured in
[`evidence/graph/live-integration-run.md`](evidence/graph/live-integration-run.md).
They are **deselected by default** — `addopts = "-ra -m 'not integration'"` in
`pyproject.toml` — so a bare `python -m pytest` never reaches for a credential.
Opt in explicitly:

```bash
pytest -m integration      # only the live tests
pytest -m ""               # offline + live
```

They read every value from the environment; nothing tenant-specific is committed:

```bash
export AZURE_TENANT_ID=...        # the Microsoft 365 tenant, not the Azure one
export AZURE_CLIENT_ID=...
export AZURE_CLIENT_SECRET=...    # never commit this, never echo it
export SHAREPOINT_SITE_ID=...     # hostname,siteGuid,webGuid
export ASSETS_LIST_ID=...
export TICKETS_LIST_ID=...
```

With any of those unset each test **skips with a reason** rather than failing, so
the suite stays green on a machine with no credentials.

They are safe to re-run. Exactly one test writes — a PATCH round-trip on a single
Assets row — and it restores the original value in a `finally` block, so the
lists are left exactly as found. Several are negative tests: a wrong client secret
must raise `GraphAuthError`, and *data* on a site the app was never granted
(`/drive`, `/drive/root/children`) plus both tenant-wide enumeration paths
(`/sites?search=*`, `/sites/getAllSites`) must all return **403**. Those 403s are
the evidence that `Sites.Selected` is genuinely scoped to one site. Note what they
deliberately do *not* assert: an ungranted site's **metadata** is readable and
returns 200, which is expected Microsoft behaviour — see
[`docs/SECURITY.md`](docs/SECURITY.md#what-sitesselected-does-and-does-not-hide).

### Run locally

```bash
cp .env.example .env      # fill in tenant, client, site and list ids
uvicorn app.main:app --reload --port 8000
python -m app.sync        # run the sync once, print a JSON summary, exit
```

### Run in Docker

```bash
docker build -t opsbridge365:local .

# API — starts and serves /healthz with no credentials at all
docker run --rm -p 8000:8000 opsbridge365:local
curl http://localhost:8000/healthz

# Sync job — same image, different command
docker run --rm --env-file .env opsbridge365:local python -m app.sync
```

`/healthz` answers `200` even with an empty environment, on purpose: a health
probe that needs secrets would have the orchestrator kill a container that is
merely unconfigured. `/metrics` — the endpoint that genuinely needs Graph
credentials — returns `503` instead, and names no variable to the caller.

### Verify everything at once

```powershell
powershell -NoProfile -File scripts/verify-opsbridge.ps1
```

A read-only report. It runs every check it can and reports the rest as **SKIP**,
never PASS — an unauthenticated run tells you exactly what it did *not* verify.

---

## Status

Three categories. **Live and verified** means it exists and was observed working.
**Built, not deployed** means the code is written and passes what can be checked
without the cloud, and nothing of it is running. **Not started** means exactly
that. Nothing is promoted a category for looking good — in particular, every Azure
resource `infra/main.bicep` declares is in the middle category, not the first.

### ✅ Live and verified

| Component | Evidence |
| --- | --- |
| Public GitHub repository, default branch `main` | [`github.com/Alhamwis/OpsBridge365`](https://github.com/Alhamwis/OpsBridge365). A disclosure review ran before it went public: zero real tenant, subscription or app identifiers in the working tree or anywhere in the commit history |
| Offline test suite | **58 passed**, integration deselected, no credentials and no network needed |
| Live integration suite against the real tenant | **12 passed** — [`evidence/graph/live-integration-run.md`](evidence/graph/live-integration-run.md). These exercise Graph auth, paging, the Assets read/PATCH round trip and the `Sites.Selected` boundary against real Microsoft Graph |
| CI `test` job on GitHub Actions | Run `32113268465` — **SUCCESS** |
| Secret scanning (gitleaks, full history) job | Run `32113268465` — **SUCCESS**. Hard gate on `build-and-push`, no `continue-on-error` |
| Container image build and push to ghcr.io | Run `32113268465` — **SUCCESS**. Published as `ghcr.io/alhamwis/opsbridge365:latest` and `:8954c91018c24774705672d146554f7c788aad32` |
| Container image, locally | Multi-stage, non-root uid 10001, `/healthz` 200 with an empty environment — [`evidence/docker/build-and-run.md`](evidence/docker/build-and-run.md) |
| Azure resource group `rg-opsbridge365` (`eastus`) | Created. **Empty** — it holds no resources from `main.bicep` |
| Deploy identity `opsbridge-deploy` | Created. Zero passwords, zero certificates, zero Graph permissions — federated OIDC only. RBAC is Contributor + Role Based Access Control Administrator, **both scoped to the resource group only**. RBAC Administrator is needed because Contributor cannot create the Key Vault Secrets User assignment in `main.bicep` |
| Budget guardrail `opsbridge-monthly-20` | $20/month on the resource group, alerts at 50% and 90% actual and 100% forecasted. Observed spend **$0.00** |
| Microsoft 365 tenant with SharePoint | M365 Business Standard, SharePoint provisioned |
| Graph identity `opsbridge-graph` | Created, holding exactly three admin-consented **application** permissions — `User.Read.All`, `Device.Read.All`, `Sites.Selected` — and zero delegated grants. Consent was granted programmatically via `appRoleAssignments`, then verified by reading the consent state back |
| `Sites.Selected` site-scoped grant | Role `write` on **one** site, `https://opsbridge365.sharepoint.com/sites/opsbridge365ops`. The boundary was then probed with the app's own token; results in [`docs/SECURITY.md`](docs/SECURITY.md#what-sitesselected-does-and-does-not-hide) |
| SharePoint `Assets` and `Tickets` lists | Provisioned by `scripts/provision_sharepoint.py`, each seeded with 4 synthetic rows. A second run reported **13 EXISTS / 0 CREATED**, which is the idempotency claim actually tested rather than asserted |
| Bootstrap privilege, granted and reversed | A throwaway `opsbridge-bootstrap` app held `Sites.FullControl.All` only long enough to create the list schema — `Sites.Selected` `write` cannot create lists — and was then **deleted**. The runtime identity never held FullControl |

### 🟡 Built, not deployed

| Component | State, and what it waits on |
| --- | --- |
| Every Azure resource in `infra/main.bicep` — Log Analytics, Container Apps environment, Key Vault, managed identity, the `opsbridge-sync` Job, the `opsbridge-api` App | **None of these exist.** The template compiles clean (Bicep CLI 0.46.1, zero diagnostics) but has never been submitted to ARM — not even `--what-if`. Waiting on a successful `deploy` job |
| `deploy.yml` deploy job | **Has run once and failed.** The cause was an OIDC subject mismatch: the job is environment-gated, so GitHub presents `repo:Alhamwis/OpsBridge365:environment:production`, while the federated credential matched `...:ref:refs/heads/main`. The app now carries exactly one federated credential, the environment-scoped one. Waiting on a re-run |
| GHCR package visibility | The image is published but the package is **private** — anonymous `docker pull` returns `unauthorized`. Container Apps pulls with no registry credentials only from a public package. Waiting on a UI-only visibility change; GitHub exposes no REST API for it |
| Operator scripts `deploy-opsbridge.ps1`, `destroy-cloud.ps1` | Written, preflight-checked, never run against Azure. `verify-opsbridge.ps1` does run end to end today and reports the cloud checks as SKIP |
| API and sync job as *running workloads* | Both are tested offline, both run in a local container, neither has ever run in Azure. Waiting on the deploy |

### ⛔ Not started

| Component | Why |
| --- | --- |
| Log Analytics failure alert | Needs a deployed workspace to attach to |
| Evidence for `evidence/azure/`, `evidence/monitoring/`, `evidence/sharepoint/`, `evidence/cost/` | Nothing has been deployed to capture. `evidence/docker/` and `evidence/graph/` are populated; the rest are empty on purpose |
| Dependency and container image scanning | No Dependabot, no CodeQL, no Trivy — see [`docs/SECURITY.md`](docs/SECURITY.md) Gaps |
| Authentication in front of `/metrics` | Public and unauthenticated by design for a demo; not acceptable for a real deployment |

**Test suite, re-run independently:**

```
$ python -m pytest -q
58 passed, 12 deselected

$ python -m pytest -m integration -q      # credentials in the environment
12 passed, 58 deselected in 10.06s
```

What this table still does **not** claim: there is no measured cold start, uptime
or throughput figure anywhere in this repository, because nothing has run in Azure
to measure. The cost figures in [`docs/COST.md`](docs/COST.md) are arithmetic over
published pricing; the only observed number is $0.00, which is what an empty
resource group costs.

---

## Tech stack

| Layer | Choice |
| --- | --- |
| Language | Python 3.12 |
| API | FastAPI + uvicorn |
| HTTP / auth | httpx (async), MSAL client-credentials flow |
| Validation | Pydantic v2, pydantic-settings |
| Tests | pytest, pytest-asyncio, respx — 58 offline tests, plus 12 opt-in live tenant tests |
| Lint | ruff (`E`, `F`, `I`, `UP`, `B`, line length 100) |
| Container | Multi-stage Dockerfile on `python:3.12-slim`, non-root uid 10001 |
| Infrastructure | Bicep, one file, resource-group scope |
| CI/CD | GitHub Actions, OIDC federated login, GHCR, gitleaks secret scan |
| Cloud | Azure Container Apps (Job + App), Key Vault, Log Analytics |
| Data | Microsoft Graph, SharePoint lists |

---

## Repo layout

```
app/                    the service
  config.py             env-driven settings; importing never raises
  graph.py              Graph client - auth, paging, retry/backoff
  sharepoint.py         list reads and asset PATCH, over graph.py
  sync.py               run-and-exit job entrypoint
  metrics.py            pure SLA computation, no I/O
  models.py             Pydantic models for Graph, lists, responses
  main.py               FastAPI app - /healthz, /metrics
tests/                  58 offline tests
  integration/          12 live tenant tests, deselected by default
infra/                  main.bicep + parameter example + infra/README.md
.github/workflows/      ci.yml (PR) and deploy.yml (main)
scripts/                deploy / verify / destroy + SharePoint provisioning
docs/                   architecture, security, cost, deployment, demo, evidence
evidence/               captured proof (see docs/EVIDENCE.md for what exists)
Dockerfile              multi-stage, non-root, HEALTHCHECK
```

## Documentation

| Document | What it covers |
| --- | --- |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Components, data flow, the two-tenant split, job-vs-API and GHCR-vs-ACR decisions |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Threat model, controls, Graph permissions, and the gaps |
| [`docs/COST.md`](docs/COST.md) | The $0-while-idle model, its assumptions, and what breaks it |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | End-to-end runbook, secrets table, the human steps |
| [`docs/DEMO.md`](docs/DEMO.md) | A 5-minute interview demo, with an offline fallback |
| [`docs/EVIDENCE.md`](docs/EVIDENCE.md) | What has been proven, how, and what has not |
| [`docs/INTERVIEW-NOTES.md`](docs/INTERVIEW-NOTES.md) | Likely questions and honest answers |
| [`infra/README.md`](infra/README.md) | Per-resource breakdown of the Bicep template |
| [`docs/SPEC-cloud-v2-AUTHORITATIVE.md`](docs/SPEC-cloud-v2-AUTHORITATIVE.md) | The design brief this was built against |

**Scope note.** This repository is the *cloud layer*. The spec also describes a
Power Platform service desk (Power Apps, SharePoint lists, Power Automate flows)
living in a separate tenant; none of that is in this repo, and nothing here
claims it is built.
