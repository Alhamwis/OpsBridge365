# Evidence

An index of what has been proven, how, and what has not.

The rule this file follows is the same one `scripts/verify-opsbridge.ps1`
follows: **a check that could not be run is reported as not run, never as a
pass.** Nothing below is reconstructed from memory or inferred from code that
"should" work.

Tenant ids, subscription ids, app ids, SharePoint site and list ids, and the Key
Vault's random name suffix appear nowhere in this repository. The API FQDN and
the GHCR image reference are public by design and appear in full.

---

## Captured artifacts

| File | Covers |
| --- | --- |
| [`evidence/azure/deployment.md`](../evidence/azure/deployment.md) | Every deployed resource in `rg-opsbridge365` (westus2), the five deployment outputs and the absence of a secret among them, the live API's measured responses, and the two Azure constraints that shaped the deployment |
| [`evidence/github-actions/pipeline.md`](../evidence/github-actions/pipeline.md) | Run **`32115509179`** — four jobs green, push-to-deploy through OIDC with no stored Azure credential — and the three earlier runs that failed for three different real reasons |
| [`evidence/monitoring/alerting.md`](../evidence/monitoring/alerting.md) | The Log Analytics capture of a cloud job run, the controlled failure used to test the alert, and **the defect that test found** |
| [`evidence/sharepoint/reconciliation.md`](../evidence/sharepoint/reconciliation.md) | The end-to-end proof: a cloud job reading Graph and writing a live SharePoint list — one confident match, three honest `Unknown`s |
| [`evidence/security/posture.md`](../evidence/security/posture.md) | Secret handling verified against the running deployment, including **Key Vault denying the human operator** with `ForbiddenByRbac` |
| [`evidence/cost/observed.md`](../evidence/cost/observed.md) | Observed spend `$0.00`, the budget that predates every resource, and why that number is weaker evidence than it looks |
| [`evidence/tests/summary.md`](../evidence/tests/summary.md) | 58 offline + 12 live, what each half covers, and what neither covers |
| [`evidence/docker/build-and-run.md`](../evidence/docker/build-and-run.md) | The container build and local runtime proof — real terminal output, ~300 lines |
| [`evidence/graph/live-integration-run.md`](../evidence/graph/live-integration-run.md) | The live integration suite against the real tenant: `12 passed, 58 deselected in 10.06s`, plus the measured `Sites.Selected` boundary |

`evidence/local/` is empty. A local end-to-end sync against the real tenant was
never captured to a file, and it no longer matters much — the same sync has now
been observed running **in Azure**, which is the stronger claim.

---

## The headline results, in one place

| Claim | Where it is proven |
| --- | --- |
| The pipeline deploys to Azure with **no stored Azure credential** | Run `32115509179`, all four jobs SUCCESS — [pipeline](../evidence/github-actions/pipeline.md) |
| The API serves live SLA numbers from **zero replicas** | `/metrics` 200 from live SharePoint; cold start **714 ms**, warm **143 ms** — [deployment](../evidence/azure/deployment.md) |
| The sync job reconciles **real Graph data into a real SharePoint list** | One device matched and PATCHed, three rows left `Unknown` — [reconciliation](../evidence/sharepoint/reconciliation.md) |
| The one secret is **never a stored value** | Key Vault reference + `secretRef`; no secret in the deployment outputs — [posture](../evidence/security/posture.md) |
| Least privilege is real, not aspirational | Key Vault returns **`ForbiddenByRbac`** to the human operator — [posture](../evidence/security/posture.md) |
| An untested alert is an assumption | The first alert query returned **0 hits** against a genuinely failed job — [alerting](../evidence/monitoring/alerting.md) |
| Idle costs nothing | Idle replica count observed **0**; spend `$0.00` against a $20 budget — [cost](../evidence/cost/observed.md) |

---

## What the container file proves

Each of these is a copied terminal transcript in the file, not a summary of one:

| Claim | Proof in the file |
| --- | --- |
| Multi-stage build succeeds | §1 — the tail of a real uncached build, through `exporting to image` |
| Runtime image has no test tooling | §1 — the `Successfully installed` line contains no pytest, respx or ruff. `pip install .`, never `.[dev]` |
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

## What the live Graph file proves

| Claim | Proof in the file |
| --- | --- |
| The app registration can acquire a real app-only token | All twelve tests authenticate with client credentials as `opsbridge-graph` |
| Real Graph paging, users and devices | The suite reads live `/users` and `/devices` |
| The Assets PATCH round trip works against a real list | `test_patch_round_trip_restores_the_original_value` mutates `AssetTag` — deliberately **not** one of the three columns the sync job owns — and restores it in a `finally` block |
| `Sites.Selected` withholds ungranted-site **data** | `/drive` and `/drive/root/children` on the ungranted tenant root → **403 accessDenied** |
| `Sites.Selected` refuses tenant-wide enumeration | `GET /sites?search=*` and `/sites/getAllSites` → **403 accessDenied** |
| `Sites.Selected` does **not** hide a site's existence | Ungranted root site metadata → **200**. Recorded because it is easy to get backwards, and an earlier test asserted 403 here and failed against a correctly configured tenant |
| The grant is real, not a coincidence of an empty tenant | A positive control: the granted site returns 200 with 3 lists and 4 Assets items |

---

## Reproducible checks — verified, no artifact captured

Treat the command as the evidence and re-run it rather than citing this page.

| Check | Command | Result |
| --- | --- | --- |
| Test suite (offline) | `python -m pytest -q` | `58 passed, 12 deselected` — no network, no credentials |
| Live tenant tests | `python -m pytest -m integration -q` | `12 passed, 58 deselected in 10.06s` |
| Bicep template compiles | `bicep build infra/main.bicep --stdout` | Exit 0, **zero diagnostics**. Bicep CLI 0.46.1 |
| `.gitignore` was the first commit | `git show --stat $(git rev-list --max-parents=0 HEAD)` | `50f92b7` — one file changed, `.gitignore` |
| SharePoint provisioning is idempotent | `python scripts/provision_sharepoint.py` (second run) | **13 EXISTS / 0 CREATED** |
| Deploy identity holds no credentials | `az ad app credential list` on `opsbridge-deploy` | Zero passwords, zero certificates. Federated OIDC only |
| Graph consent state | Read back via `appRoleAssignments` after granting it programmatically | Exactly three application permissions: `User.Read.All`, `Device.Read.All`, `Sites.Selected`. Zero delegated grants |
| Sync job trigger type | `az containerapp job show ... --query properties.configuration.triggerType` | `Schedule` — also asserted by the pipeline's own post-deploy step |

---

## What is *not* proven

Said plainly, so nobody has to infer it from an absence:

- **Cost over a full billing cycle.** `$0.00` is real, and it partly reflects a
  subscription that is only hours old. One month at this configuration would turn
  the arithmetic in [`COST.md`](COST.md) into evidence; nothing shorter does.
- **Behaviour at scale.** The end-to-end proof ran against **one user and one
  device**. The matching rules, the paging and the throttling handling are
  covered by offline tests, but nothing here has met a fleet, real Graph
  throttling at volume, or a large SharePoint list.
- **`/metrics` under load.** One cold request and one warm request are measured.
  There is no throughput, concurrency or sustained-latency figure.
- **Uptime.** The deployment is hours old. No availability claim is made.
- **The alert firing end to end to a human.** The corrected query was verified to
  return 2 hits against the real failure, and the action group exists. A
  delivered notification is a further step and is not claimed.
- **`destroy-cloud.ps1` against the live resource group.** Written and
  preflight-checked; deleting the deployment to prove the teardown works has not
  been done.
- **Dependency and image scanning.** Not implemented — no Dependabot, no CodeQL,
  no Trivy. Secret scanning is implemented and gating.

---

## How to regenerate what can be checked

Offline, on any machine with Python 3.12 and Docker:

```bash
python -m pytest -q                                    # 58 passed, 12 deselected
bicep build infra/main.bicep --stdout > /dev/null       # 0 diagnostics
docker build -t opsbridge365:local .
docker run --rm opsbridge365:local id -u                # 10001
```

Against the live deployment, no credentials needed — the API is public:

```bash
curl -s -i https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io/healthz
curl -s    https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io/metrics
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
