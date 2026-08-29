# Evidence

An index of what was proven, how, and what was not.

> **Everything indexed here is historical.** Every file under `evidence/` is a
> point-in-time capture — mostly **2026-08-18**, one from **2026-08-16**. Not one
> of them is a live status page. Where a figure has since been re-measured or a
> claim overtaken, the correction sits inline in that file next to what was
> recorded on the day, and the same corrections are collected
> [below](#where-these-captures-have-since-been-overtaken). For what is true about
> the deployed system *today*, and the command that checked it, read
> [`STATUS.md`](STATUS.md).

The rule this file follows is the same one `scripts/verify-opsbridge.ps1`
follows: **a check that could not be run is reported as not run, never as a
pass.** Nothing below is reconstructed from memory or inferred from code that
"should" work.

Tenant ids, subscription ids, app ids, SharePoint site and list ids, and the Key
Vault's random name suffix appear nowhere in this repository. The API FQDN and
the GHCR image reference are public by design and appear in full.

---

## Captured artifacts

**Nine files.** `evidence/` holds ten directories; nine contain one capture each
and `evidence/local/` is empty. Every file carries a header stating when it was
captured.

| File | Captured | Covers |
| --- | --- | --- |
| [`evidence/azure/deployment.md`](../evidence/azure/deployment.md) | 2026-08-18 | Every resource that existed in `rg-opsbridge365` (westus2) on the day, the five deployment outputs and the absence of a secret among them, the API's measured responses, and the two Azure constraints that shaped the deployment |
| [`evidence/github-actions/pipeline.md`](../evidence/github-actions/pipeline.md) | 2026-08-18 | The **first** fully green deploy — run `32115509179` at commit `63c4616`, four jobs green, push-to-deploy through OIDC with no stored Azure credential — and the four failures before it |
| [`evidence/monitoring/alerting.md`](../evidence/monitoring/alerting.md) | 2026-08-18 | The Log Analytics capture of a cloud job run, the controlled failure used to test the alert, and **the defect that test found** |
| [`evidence/sharepoint/reconciliation.md`](../evidence/sharepoint/reconciliation.md) | 2026-08-18 | The end-to-end proof: a cloud job reading Graph and writing a live SharePoint list — one confident match, three honest `Unknown`s |
| [`evidence/security/posture.md`](../evidence/security/posture.md) | 2026-08-18 | Secret handling checked against the running deployment, including **Key Vault denying the human operator** with `ForbiddenByRbac` |
| [`evidence/cost/observed.md`](../evidence/cost/observed.md) | 2026-08-18 | Observed spend `$0.00` on that date, the budget that predates every resource, and why that number is weaker evidence than it looks |
| [`evidence/tests/summary.md`](../evidence/tests/summary.md) | 2026-08-18 | The suite as it stood: 58 offline + 12 live, what each half covered, and what neither covered. The offline suite is **106** today — see [`STATUS.md`](STATUS.md) |
| [`evidence/docker/build-and-run.md`](../evidence/docker/build-and-run.md) | 2026-08-16 | The container build and local runtime proof — verbatim terminal output, and a list of what has changed in the Dockerfile since |
| [`evidence/graph/live-integration-run.md`](../evidence/graph/live-integration-run.md) | 2026-08-18 | The live integration suite against the real tenant: `12 passed, 58 deselected in 10.06s`, plus the measured `Sites.Selected` boundary |

Nothing was ever written to `evidence/local/`: a local end-to-end sync against
the real tenant was never captured to a file. It matters less than it did — the
same sync was observed running **in Azure**, which is the stronger claim.

---

## Where these captures have since been overtaken

Three of them record numbers or states that are no longer true. Each file keeps
what it recorded on the day and carries the correction inline; they are collected
here so nobody quotes a superseded figure from the index.

| Captured claim | Correction |
| --- | --- |
| Cold start from zero replicas **714 ms**, warm **143 ms** (2026-08-18) | Re-measured **2026-08-29** with the replica count observed at 0 immediately beforehand: **20.2 s** cold, **248 ms** warm; a second run gave 21.3 s. 714 ms cannot have been measured against a sleeping app |
| `/metrics` → 200 with `sla_compliance_7d_pct: 50.0, resolved_last_7d: 2` | The 7-day window has rolled past those resolutions. The field is now `null` with a denominator of 0, which is the designed behaviour, not a fault. `/metrics` also now requires a bearer token — **401** without one |
| Run `32115509179` as *the* successful deploy | It was the **first**. Later runs deployed too. Current deploy state: the [Actions run list](https://github.com/Alhamwis/OpsBridge365/actions/workflows/deploy.yml) |

---

## The headline results, in one place

Every row is an observation from the capture date in the middle column.

| Claim | When | Where it is proven |
| --- | --- | --- |
| The pipeline deploys to Azure with **no stored Azure credential** | 2026-08-18 | Run `32115509179`, all four jobs SUCCESS — [pipeline](../evidence/github-actions/pipeline.md) |
| The API served live SLA numbers **from zero replicas** | 2026-08-18 | `/metrics` 200 computed from live SharePoint, on a replica that did not exist before the request — [deployment](../evidence/azure/deployment.md). The latency figures in that file are corrected above |
| The sync job reconciles **real Graph data into a real SharePoint list** | 2026-08-18 | One device matched and PATCHed, three rows left `Unknown` — [reconciliation](../evidence/sharepoint/reconciliation.md) |
| The Graph secret is **never a stored value** on the app resource | 2026-08-18 | Key Vault reference + `secretRef`; no secret in the deployment outputs — [posture](../evidence/security/posture.md) |
| Least privilege is real, not aspirational | 2026-08-18, re-verified 2026-08-29 | Key Vault returns **`ForbiddenByRbac`** to the human operator — [posture](../evidence/security/posture.md) |
| An untested alert is an assumption | 2026-08-18 | The first alert query returned **0 hits** against a genuinely failed job — [alerting](../evidence/monitoring/alerting.md) |
| Idle costs nothing | 2026-08-18 | Idle replica count observed **0**; spend `$0.00` against a $20 budget — [cost](../evidence/cost/observed.md) |

---

## What the container file proves

Captured 2026-08-16. Each of these is a copied terminal transcript in the file,
not a summary of one.

| Claim | Proof in the file |
| --- | --- |
| Multi-stage build succeeds | §1 — the tail of a real uncached build, through `exporting to image` |
| Runtime image has no test tooling | §1 — the `Successfully installed` line contains no pytest, respx or ruff |
| The container runs as non-root | §2 — `docker run ... id -u` → `10001`, and `docker exec` on the *live server container* → `10001` |
| The runtime filesystem is not writable | §2 — `touch /srv/probe` → `Permission denied` |
| `/healthz` answers with **no credentials at all** | §3 — container started with no `--env-file` and no `-e`; `HTTP/1.1 200` with `{"status":"ok","version":"0.1.0"}` |
| `/metrics` refuses without leaking variable names | §3 — `HTTP 503` `{"detail":"Service configuration is incomplete."}`, while the container log names all six missing variables |
| Docker's `HEALTHCHECK` works via stdlib `urllib` | §3 — `docker inspect` reports `healthy`, exit 0 |
| One image, two entrypoints | §3 — `docker run ... python -m app.sync` runs and exits `2` on the documented `config_error` path |
| Image size | §4 — 281 MB on disk, 66.6 MB content size |
| No secrets, tests, docs or `.git` in the image | §5 — `/srv` contains only `app`, and `.env` does not exist |

The file also documents two code changes made *because* containerising exposed
problems, with the failing output that motivated each: `/healthz` needed to
answer without configuration, and `pyproject.toml` referenced a `README.md` that
did not exist, which broke `pip install .`.

The Dockerfile has changed since that capture — the base image is now pinned by
digest and dependencies install from a hash-pinned lock with `--require-hashes`.
The transcript is not a description of today's build. The file says so at the top.

## What the live Graph file proves

Captured 2026-08-18, against a tenant that is an **`O365_BUSINESS_PREMIUM`
trial** with a lifecycle date of **2026-09-16**. After that date this suite is
not reproducible unless the subscription is continued.

| Claim | Proof in the file |
| --- | --- |
| The app registration can acquire a real app-only token | All twelve tests authenticated with client credentials as `opsbridge-graph` |
| Real Graph paging, users and devices | The suite reads live `/users` and `/devices` |
| The Assets PATCH round trip works against a real list | `test_patch_round_trip_restores_the_original_value` mutates `AssetTag` — deliberately **not** one of the three columns the sync job owns — and restores it in a `finally` block |
| `Sites.Selected` withholds ungranted-site **data** | `/drive` and `/drive/root/children` on the ungranted tenant root → **403 accessDenied** |
| `Sites.Selected` refuses tenant-wide enumeration | `GET /sites?search=*` and `/sites/getAllSites` → **403 accessDenied** |
| `Sites.Selected` does **not** hide a site's existence | Ungranted root site metadata → **200**. Recorded because it is easy to get backwards, and an earlier test asserted 403 here and failed against a correctly configured tenant |
| The grant is real, not a coincidence of an empty tenant | A positive control: the granted site returned 200 with 3 lists and 4 Assets items |

---

## Reproducible checks — run 2026-08-18, no artifact captured

Treat the command as the evidence and re-run it rather than citing this page. The
results below are what those commands returned on **2026-08-18**; the rows marked
† were re-run on 2026-08-29 and their current results are in
[`STATUS.md`](STATUS.md).

| Check | Command | Result on 2026-08-18 |
| --- | --- | --- |
| Test suite (offline) † | `python -m pytest -q` | `58 passed, 12 deselected` — no network, no credentials. **106 passed** on 2026-08-29 |
| Live tenant tests | `python -m pytest -m integration -q` | `12 passed, 58 deselected in 10.06s` |
| Bicep template compiles † | `bicep build infra/main.bicep --stdout` | Exit 0, **zero diagnostics**. Bicep CLI 0.46.1 |
| `.gitignore` was the first commit | `git show --stat $(git rev-list --max-parents=0 HEAD)` | `50f92b7` — one file changed, `.gitignore` |
| SharePoint provisioning is idempotent | `python scripts/provision_sharepoint.py` (second run) | **13 EXISTS / 0 CREATED** |
| Deploy identity holds no credentials | `az ad app credential list` on `opsbridge-deploy` | Zero passwords, zero certificates. Federated OIDC only |
| Graph consent state | Read back via `appRoleAssignments` after granting it programmatically | Exactly three application permissions: `User.Read.All`, `Device.Read.All`, `Sites.Selected`. Zero delegated grants |
| Sync job trigger type † | `az containerapp job show ... --query properties.configuration.triggerType` | `Schedule` — also asserted by the pipeline's own post-deploy step |

---

## What is *not* proven

Said plainly, so nobody has to infer it from an absence.

- **Cost over a full billing cycle.** `$0.00` was observed on **2026-08-18**,
  when the subscription was hours old, and has not been re-observed since. One
  month at this configuration would turn the arithmetic in
  [`COST.md`](COST.md) into evidence; nothing shorter does.
- **Behaviour at scale.** The end-to-end proof ran against **one user and one
  device**. The matching rules, the paging and the throttling handling are
  covered by offline tests, but nothing here has met a fleet, real Graph
  throttling at volume, or a large SharePoint list.
- **`/metrics` under load.** Cold and warm latency are two data points. There is
  no throughput, concurrency or sustained-latency figure, and the 45-second cache
  and the per-caller rate limit have been exercised by offline tests only, never
  against the deployed service under concurrent traffic.
- **Uptime.** No availability percentage is published. The scheduled health
  workflow probes every four hours; six samples a day cannot support a
  percentage, and inventing one would be worse than having none.
- **The alert firing end to end to a human.** The corrected query was verified to
  return 2 hits against the real failure, and the action group exists. A
  delivered notification is a further step and is not claimed.
- **`destroy-cloud.ps1` against the live resource group.** Written and
  preflight-checked; deleting the deployment to prove the teardown works has not
  been done.
- **The repository hardening applied in this release.** Branch protection on
  `main`, the production environment's protection rules, secret scanning and push
  protection are configured state, not something captured under `evidence/`. The
  gap list in [`../evidence/security/posture.md`](../evidence/security/posture.md)
  records what was true on 2026-08-18.

Dependency and image scanning **were** listed here as not implemented. They now
exist — CodeQL, Trivy filesystem and image scans, an SPDX SBOM, and Dependabot —
and they are described in [`SECURITY.md`](SECURITY.md), not proven by a capture
under `evidence/`.

---

## How to regenerate what can be checked

These are current commands, not a transcript. Offline, on any machine with
Python 3.12 and Docker:

```bash
pip install --require-hashes --no-deps -r requirements-dev.txt
pip install --no-deps -e .
python -m pytest -q                                     # 106 passed, 12 deselected
az bicep build --file infra/main.bicep                  # 0 diagnostics
az bicep build --file infra/bootstrap.bicep             # 0 diagnostics
docker build -t opsbridge365:local .
docker run --rm opsbridge365:local id -u                # 10001
```

Against the live deployment. `/healthz` and `/demo/metrics` are public;
`/metrics` needs the bearer token and returns 401 without it:

```bash
BASE=https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io
curl -s -i "$BASE/healthz"
curl -s    "$BASE/demo/metrics"                                  # "synthetic": true
curl -s -o /dev/null -w '%{http_code}\n' "$BASE/metrics"         # 401
curl -s -H "Authorization: Bearer $METRICS_API_TOKEN" "$BASE/metrics"
curl -s -o /dev/null -w '%{http_code}\n' http://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io/healthz   # 301
```

With tenant credentials in the environment:

```bash
python -m pytest -m integration -q                      # 12 passed
python scripts/provision_sharepoint.py --dry-run        # plan only, sends nothing
```

Or the whole local half at once, with an honest SKIP for anything unavailable:

```powershell
powershell -NoProfile -File scripts/verify-opsbridge.ps1
```
