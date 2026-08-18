# Evidence

An index of what has actually been proven, how, and what has not.

The rule this file follows is the same one `scripts/verify-opsbridge.ps1` follows:
**a check that could not be run is reported as not run, never as a pass.** Nothing
below is reconstructed from memory or inferred from code that "should" work.

---

## Captured artifacts

There are **two** captured evidence files in the repository. Every other directory
under `evidence/` is empty, and the table further down says why.

| File | Covers | Captured |
| --- | --- | --- |
| [`evidence/docker/build-and-run.md`](../evidence/docker/build-and-run.md) | The full container build and local runtime proof — real terminal output, ~300 lines | Windows 11 Pro 26200, Docker 29.1.3 |
| [`evidence/graph/live-integration-run.md`](../evidence/graph/live-integration-run.md) | The live integration suite against the real Microsoft 365 tenant: `12 passed, 58 deselected in 10.06s`, plus the measured `Sites.Selected` boundary | Run with the `opsbridge-graph` application identity against the live tenant |

### What the container file proves

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
| Image size | §4 — 281 MB on disk, 66.6 MB content size (`docker image inspect` → `66605912`) |
| No secrets, tests, docs or `.git` in the image | §5 — directory listings showing `/srv` contains only `app`, and `.env` does not exist |
| `.dockerignore` is effective | §1 — build context transferred is `979B` |

The file also documents two code changes made *because* containerising exposed
problems, with the failing output that motivated each: `/healthz` needed to answer
without configuration (or Docker's healthcheck would kill an unconfigured
container), and `pyproject.toml` referenced a `README.md` that did not exist, which
broke `pip install .` before any container work.

### What the live Graph file proves

This is the file that moved the Graph half of the system from "tested against
mocks" to "tested against Microsoft".

| Claim | Proof in the file |
| --- | --- |
| The app registration can acquire a real app-only token | All twelve tests authenticate with client credentials as `opsbridge-graph`; none would run otherwise |
| Real Graph paging, users and devices | The suite reads live `/users` and `/devices` — the code paths previously exercised only against `respx` fixtures |
| The Assets PATCH round trip works against a real list | `test_patch_round_trip_restores_the_original_value` mutates `AssetTag` — deliberately **not** one of the three columns the sync job owns — and restores it in a `finally` block, so no test leaves data changed |
| `Sites.Selected` withholds ungranted-site **data** | `/drive` and `/drive/root/children` on the ungranted tenant root → **403 accessDenied** |
| `Sites.Selected` refuses tenant-wide enumeration | `GET /sites?search=*` and `/sites/getAllSites` → **403 accessDenied** |
| `Sites.Selected` does **not** hide a site's existence | Ungranted root site metadata → **200**. Recorded because it is easy to get backwards, and an earlier test asserted 403 here and failed against a correctly configured tenant |
| The grant is real, not a coincidence of an empty tenant | A positive control: the granted site returns 200 with 3 lists and 4 Assets items. A 403 proves nothing unless the same call is shown to succeed where a grant exists |

Tenant, app and site identifiers are deliberately omitted from that file, as they
are from every file in this repository.

---

## Reproducible checks — verified, no artifact captured

These are checks whose output was observed but not written to a file under
`evidence/`. Treat the command as the evidence and re-run it rather than citing
this page.

| Check | Command | Result |
| --- | --- | --- |
| Test suite (offline) | `python -m pytest -q` | `58 passed, 12 deselected` — no network, no credentials |
| Live tenant tests | `python -m pytest -m integration -q` | `12 passed, 58 deselected in 10.06s` (captured — see above) |
| Bicep template compiles | `bicep build infra/main.bicep --stdout` | Exit 0, **zero diagnostics**. Bicep CLI 0.46.1 |
| `.gitignore` was the first commit | `git show --stat $(git rev-list --max-parents=0 HEAD)` | `50f92b7` — one file changed, `.gitignore`, 19 insertions |
| SharePoint provisioning is idempotent | `python scripts/provision_sharepoint.py` (second run) | **13 EXISTS / 0 CREATED** — nothing recreated, nothing modified |
| Deploy identity holds no credentials | `az ad app credential list` on `opsbridge-deploy` | Zero passwords, zero certificates. Federated OIDC only |
| Graph consent state | Read back via `appRoleAssignments` after granting it programmatically | Exactly three application permissions: `User.Read.All`, `Device.Read.All`, `Sites.Selected`. Zero delegated grants |
| Observed Azure spend | Cost Management on `rg-opsbridge365` | **$0.00** — which is what an empty resource group costs |

---

## GitHub Actions — what has run, and what has not

Actions run **32113268465**, on `main`:

| Job | Result |
| --- | --- |
| `test` | ✅ SUCCESS |
| `secret scan (gitleaks)` | ✅ SUCCESS — full history, `fetch-depth: 0`, hard gate |
| `build and push to ghcr.io` | ✅ SUCCESS — `ghcr.io/alhamwis/opsbridge365:latest` and `:8954c91018c24774705672d146554f7c788aad32` |
| `deploy to Azure` | ❌ **FAILED** |

The deploy failure was an **OIDC subject mismatch**, not an infrastructure
problem: the deploy job is gated on the `production` GitHub environment, so the
token GitHub presents carries the subject
`repo:Alhamwis/OpsBridge365:environment:production`, while the federated
credential on `opsbridge-deploy` matched `repo:Alhamwis/OpsBridge365:ref:refs/heads/main`.
The app now holds exactly one federated credential, the environment-scoped one.
**The workflow has not been re-run since the fix**, so no successful deploy exists
and nothing below should be read as though one does. `docs/DEPLOYMENT.md`
documents the gotcha in full.

---

## Empty by design — nothing has been deployed

These directories exist and are **empty**. Each waits on a deployment that has not
happened.

| Directory | Would hold | Waiting on |
| --- | --- | --- |
| `evidence/azure/` | Deployment output, resource list | A successful `deploy` job — the resource group exists but is empty |
| `evidence/github-actions/` | A fully green pipeline run | The deploy job passing; three of four jobs already do |
| `evidence/sharepoint/` | The Assets list before and after a real sync | The sync job running in Azure. The lists themselves exist and are seeded |
| `evidence/monitoring/` | Log Analytics queries, a fired alert | A deployed Log Analytics workspace |
| `evidence/cost/` | A Cost Management export | Something billable to export. Observed spend is $0.00 against a $20 budget |
| `evidence/security/` | Role assignment listings, Key Vault access proof | A deployed vault. The RBAC on `opsbridge-deploy` is verified but not captured to a file |
| `evidence/local/` | Local end-to-end sync against the real tenant | Nothing blocking — the credentials exist; simply not captured |
| `evidence/tests/` | A captured pytest transcript | Nothing blocking — simply not captured to a file |

---

## What is therefore **not** proven

Said plainly, so nobody has to infer it from an absence:

- **Nothing from `infra/main.bicep` has ever run in Azure.** No Log Analytics
  workspace, no Container Apps environment, no Key Vault, no managed identity, no
  Job, no API. `bicep build` proves the template compiles; it does not prove ARM
  accepts it, that the Key Vault reference resolves, that the role assignment
  propagates, or that ingress comes up. `az deployment group validate` and
  `--what-if` have not been run.
- **The deploy workflow has never succeeded.** It has run once and failed, for the
  reason above. The fix is in place and unexercised.
- **The published image cannot be pulled anonymously.** The GHCR package is
  private; `docker pull` without credentials returns `unauthorized`. Container Apps
  pulls with no registry credential only from a public package, so this is a
  prerequisite for the deploy, not a cosmetic setting. GitHub exposes no REST API
  for container package visibility — it is a UI-only change.
- **`deploy-opsbridge.ps1` and `destroy-cloud.ps1` have never run against Azure.**
  `verify-opsbridge.ps1` does run end to end today, and reports every cloud check
  as SKIP.
- **No performance, cold-start, uptime, or throughput number exists anywhere in
  this repository.** Nothing has run in the right place to measure, and no such
  figure has been estimated and presented as measured. The cost figures in
  [`COST.md`](COST.md) are arithmetic over published pricing, explicitly labelled
  as such; the one observed number is $0.00.

What *is* now proven, and was not before: real Graph authentication, real paging,
a real PATCH round trip, and the actual shape of the `Sites.Selected` boundary.
Those moved out of this list because twelve tests ran against Microsoft, not
against `respx`.

---

## How to regenerate everything that can be proven today

Offline, on any machine with Python 3.12 and Docker:

```bash
python -m pytest -q                                    # 58 passed, 12 deselected
bicep build infra/main.bicep --stdout > /dev/null       # 0 diagnostics
docker build -t opsbridge365:local .
docker run --rm opsbridge365:local id -u                # 10001
docker run -d --rm --name ev -p 8000:8000 opsbridge365:local
curl -s -i http://localhost:8000/healthz                # 200
curl -s -w '\nHTTP %{http_code}\n' http://localhost:8000/metrics   # 503
docker stop ev
```

With tenant credentials in the environment (see the Quickstart in the README):

```bash
python -m pytest -m integration -q                      # 12 passed
python scripts/provision_sharepoint.py --dry-run        # plan only, sends nothing
```

Or all of the offline half at once, with an honest SKIP for anything unavailable:

```powershell
powershell -NoProfile -File scripts/verify-opsbridge.ps1
```

The empty directories above are the remaining checklist. Once the GHCR package is
public and the deploy job is re-run, capture the deployment outputs, the green
pipeline run, a job execution log, the Assets list before and after a real sync,
and a Cost Management export.
