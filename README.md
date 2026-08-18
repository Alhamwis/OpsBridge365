# OpsBridge365 — cloud layer

OpsBridge365 keeps an IT service desk's device inventory honest: a scheduled job
pulls users and devices from Microsoft Graph and writes them into a SharePoint
**Assets** list, and a small HTTP API reads the **Tickets** list and returns live
SLA numbers. It is packaged as one container image with two entrypoints, deployed
by one Bicep template, and designed to cost nothing while idle.

> **Deployment status, stated up front. It is deployed, and it is running.**
> GitHub Actions run [`32115509179`](#status) was green end to end — test, secret
> scan, image push, and `deploy to Azure` — through OIDC federation with **no
> stored Azure credential**. Everything `infra/main.bicep` declares now exists in
> `rg-opsbridge365` (westus2): Log Analytics, a user-assigned identity, Key Vault,
> a Container Apps environment, the `opsbridge-sync` Job on a cron, and the
> `opsbridge-api` App. The API is public and answering:
>
> ```
> https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io/healthz
> ```
>
> The sync job has run in the cloud and written a live SharePoint list. The
> [status table](#status) records what was measured, and the
> [gaps](#known-gaps--not-done) record what still is not done — including the
> alert defect that testing found, which is written up rather than quietly fixed.

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

### Call the live API — no credentials, no setup

```bash
BASE=https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io

curl -s "$BASE/healthz"    # {"status":"ok","version":"0.1.0"}
curl -s "$BASE/metrics"    # live SLA numbers, read from SharePoint
```

If the first call takes about three quarters of a second and the second is
instant, that is `minReplicas: 0` doing its job — measured at **714 ms** cold and
**143 ms** warm. Plain `http://` returns **301**; ingress sets
`allowInsecure: false`.

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

Every row below was observed working against the real thing, and every number in
it was measured rather than estimated. Where something was *not* measured, it is
in [Known gaps](#known-gaps--not-done) instead — nothing is promoted for looking
good.

| Component | Status | Evidence |
| --- | --- | --- |
| **CI/CD, push to deploy** | ✅ Live | Actions run **`32115509179`**: `test` SUCCESS, `secret scan (gitleaks)` SUCCESS, `build and push to ghcr.io` SUCCESS, `deploy to Azure` SUCCESS. Full green through **OIDC federation with no stored Azure credential** — [`evidence/github-actions/pipeline.md`](evidence/github-actions/pipeline.md) |
| **Azure deployment** | ✅ Live | Resource group `rg-opsbridge365` in **westus2**: Log Analytics `opsbridge-logs` (PerGB2018, 30-day retention), user-assigned identity `opsbridge-id`, Key Vault (RBAC-enabled, soft-delete on, standard), Container Apps environment `opsbridge-env`, Job `opsbridge-sync`, App `opsbridge-api` — [`evidence/azure/deployment.md`](evidence/azure/deployment.md) |
| **Live API** | ✅ Live | `https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io` — `/healthz` → **200** `{"status":"ok","version":"0.1.0"}`. **Cold start from zero replicas: 714 ms.** Warm: **143 ms** |
| **Live SLA metrics** | ✅ Live | `/metrics` → **200**, computed from live SharePoint: `{"open_tickets":2,"due_within_30min":0,"sla_compliance_7d_pct":50.0,"resolved_last_7d":2,"sla_measured_last_7d":2}` |
| **HTTPS only** | ✅ Verified | `http://` → **301** to `https://`. Ingress `allowInsecure: false` |
| **Scale to zero** | ✅ Verified | `minReplicas: 0`, `maxReplicas: 1`, cpu 0.25 / memory 0.5Gi. **Idle replica count observed: 0** |
| **Scheduled sync job** | ✅ Verified | `triggerType: Schedule`, cron `0 */6 * * *`, `replicaRetryLimit: 1`, `replicaTimeout: 1800`. **The schedule was proven, not assumed**: cron was temporarily set to `*/5`, execution `opsbridge-sync-29784065` started at `09:05:00Z` by Azure's scheduler with no human or local involvement and succeeded, then the production cron was restored and read back |
| **End-to-end reconciliation** | ✅ Verified | The cloud job read Graph and wrote the live Assets list: `users_fetched: 1, devices_fetched: 1, assets_fetched: 4, matched: 1, patched: 1, unknown_last_check_in: 1`. **One confident match written, three honest `Unknown`s** — [`evidence/sharepoint/reconciliation.md`](evidence/sharepoint/reconciliation.md) |
| **Secret handling** | ✅ Verified | Container App secret `graph-client-secret` is a **Key Vault reference** (`keyVaultUrl` set, **no inline value**); `AZURE_CLIENT_SECRET` reaches the container as a `secretRef`. Deployment outputs are exactly `apiFqdn, identityPrincipalId, jobName, keyVaultName, logAnalyticsWorkspaceId` — **no secret** |
| **Least privilege, demonstrated** | ✅ Verified | Key Vault denies the **human operator** with **`ForbiddenByRbac`**. Only the managed identity holds Key Vault Secrets User — [`evidence/security/posture.md`](evidence/security/posture.md) |
| **Log Analytics** | ✅ Live | Captured the cloud job's summary from a container that no longer exists |
| **Failure alerting** | ✅ Live, and it found a defect | Action group `opsbridge-alerts` + scheduled query rule `opsbridge-sync-failed` (severity 2, 5-min evaluation, 15-min window). Tested with a **controlled failure**; the first version of the query returned **0 hits against a genuinely failed job**. Corrected query returns **2** — [`evidence/monitoring/alerting.md`](evidence/monitoring/alerting.md) |
| **Cost** | ✅ Verified | Observed spend **$0.00**. Budget `opsbridge-monthly-20` at $20/month, alerts at 50%/90% actual and 100% forecasted, **created before any billable resource** — [`evidence/cost/observed.md`](evidence/cost/observed.md) |
| **Offline test suite** | ✅ Verified | **58 passed**, integration deselected, no credentials and no network |
| **Live integration suite** | ✅ Verified | **12 passed** against the real tenant — Graph auth, paging, the Assets PATCH round trip and the `Sites.Selected` boundary — [`evidence/graph/live-integration-run.md`](evidence/graph/live-integration-run.md) |
| **Container image** | ✅ Live | `ghcr.io/alhamwis/opsbridge365`, public, pulled by Container Apps with **no registry credential**. Multi-stage, non-root uid 10001 — [`evidence/docker/build-and-run.md`](evidence/docker/build-and-run.md) |
| **Secret scanning** | ✅ Live | gitleaks over the **full history** (`fetch-depth: 0`), hard gate on `build-and-push`, no `continue-on-error`. Allowlist holds 7 public Microsoft GUIDs, matched **by exact value only** |
| **Deploy identity `opsbridge-deploy`** | ✅ Verified | Zero passwords, zero certificates, zero Graph permissions — one federated credential, subject `repo:Alhamwis@<ownerId>/OpsBridge365@<repoId>:environment:production`. RBAC is Contributor + Role Based Access Control Administrator, **resource-group scope only** |
| **Graph identity `opsbridge-graph`** | ✅ Verified | Exactly three admin-consented **application** permissions — `User.Read.All`, `Device.Read.All`, `Sites.Selected` — zero delegated grants. Consent granted programmatically via `appRoleAssignments`, then read back |
| **`Sites.Selected` boundary** | ✅ Verified | Role `write` on **one** site. Ungranted-site *data* → 403, tenant-wide enumeration → 403, ungranted-site *metadata* → 200 (expected Microsoft behaviour, documented rather than overclaimed) |
| **SharePoint `Assets` / `Tickets`** | ✅ Live | Provisioned and seeded with 4 synthetic rows each. A second provisioning run reported **13 EXISTS / 0 CREATED** |
| **Public repository** | ✅ Live | [`github.com/Alhamwis/OpsBridge365`](https://github.com/Alhamwis/OpsBridge365). Root commit `50f92b7` is `.gitignore` alone, so no secret could predate it. A disclosure review passed before it went public |

### ⚠️ One dated action, not automatable

The Microsoft 365 tenant runs on an **O365_BUSINESS_PREMIUM trial** — Microsoft Graph reports
`isTrial: true`, created **2026-08-18**, with `nextLifecycleDateTime` of **2026-09-16** and 25
licences. Before that date, turn off recurring billing in the Microsoft 365 admin center
(*Billing → Your products*), or it converts to paid. Microsoft exposes no supported Graph or CLI
API for this, so it cannot be scripted.

Turning off renewal does **not** tear anything down immediately — the tenant, the SharePoint site
and the Graph identity keep working until the trial period ends. The Azure side is unaffected: it
lives in a different tenant and costs $0.00.

### Known gaps — not done

| Gap | Why it matters |
| --- | --- |
| **Cost is not proven over a billing cycle** | `$0.00` is real, and it partly reflects a subscription only hours old. Expected steady state is $0.00/month on the assumptions in [`docs/COST.md`](docs/COST.md); one full month at this configuration is what would turn arithmetic into evidence |
| **The end-to-end run was one user and one device** | The matching and paging rules are covered by offline tests, but nothing here has met a real fleet, real Graph throttling at volume, or a large list |
| **No load, throughput or uptime figure** | One cold request and one warm request were measured. That is a latency data point, not a performance characterisation, and the deployment is hours old |
| **`/metrics` is unauthenticated** | Public by design for a demo. It exposes counts and a percentage — no ticket contents, no personal data — and would not be acceptable in production |
| **No dependency or image scanning** | No Dependabot, no CodeQL, no Trivy. Secret scanning *is* implemented and gating. GitHub push protection is still off |
| **Key Vault allows public network access** | Private endpoints and a vault firewall are the production answer; neither is free |
| **Teardown never exercised** | `destroy-cloud.ps1` is written and preflight-checked, and has not been run against the live resource group |

### How it got here — four real failures

The deploy did not work first time. Three earlier runs failed for three
genuinely different reasons, none of them a code defect, and each is written up
with its fix in
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md#things-that-will-bite-you):

1. **`AADSTS700213`** — the deploy job is environment-gated, so GitHub presents
   `...:environment:production`, not `...:ref:refs/heads/main`.
2. **`AADSTS700213` again** — this account's GitHub default subject is
   **ID-qualified**: `repo:Alhamwis@<ownerId>/OpsBridge365@<repoId>:environment:production`.
   `use_default` was already `true`, so there was nothing to normalise; Entra has
   to match the ID-qualified string. That form is rename-proof, which is a
   security improvement rather than a workaround.
3. **`RequestDisallowedByAzure`** — Azure for Students enforces an allowed-regions
   policy. Permitted: `northcentralus`, `mexicocentral`, `westus2`, `westus`,
   `canadacentral`. **`eastus` is not allowed.** The resource group was recreated
   in `westus2`.
4. **`MissingSubscriptionRegistration`** — `Microsoft.App`, `Microsoft.KeyVault`,
   `Microsoft.OperationalInsights`, `Microsoft.ManagedIdentity` and
   `Microsoft.Insights` were all unregistered on a fresh subscription.

`bicep build` returned zero diagnostics throughout. The template was never the
problem — every failure lived between the repository and one specific real
subscription, which is the argument for deploying rather than reasoning about
deploying.

**Test suite, re-run independently:**

```
$ python -m pytest -q
58 passed, 12 deselected

$ python -m pytest -m integration -q      # credentials in the environment
12 passed, 58 deselected in 10.06s
```

One thing the table above deliberately does not do is treat `$0.00` as settled.
It is an observed number against a subscription only hours old; the expected
steady state and everything that would break it are in
[`docs/COST.md`](docs/COST.md).

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
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | End-to-end runbook, secrets table, and **[things that will bite you](docs/DEPLOYMENT.md#things-that-will-bite-you)** — the four real deploy failures and their fixes |
| [`docs/DEMO.md`](docs/DEMO.md) | A 5-minute interview demo, with an offline fallback |
| [`docs/EVIDENCE.md`](docs/EVIDENCE.md) | Index of the nine files under `evidence/` — what was proven, how, and what has not been |
| [`docs/INTERVIEW-NOTES.md`](docs/INTERVIEW-NOTES.md) | Likely questions and honest answers |
| [`infra/README.md`](infra/README.md) | Per-resource breakdown of the Bicep template |
| [`docs/SPEC-cloud-v2-AUTHORITATIVE.md`](docs/SPEC-cloud-v2-AUTHORITATIVE.md) | The design brief this was built against |

**Scope note.** This repository is the *cloud layer*. The spec also describes a
Power Platform service desk (Power Apps, SharePoint lists, Power Automate flows)
living in a separate tenant; none of that is in this repo, and nothing here
claims it is built.
