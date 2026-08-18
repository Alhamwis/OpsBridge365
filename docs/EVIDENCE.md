# Evidence

An index of what has actually been proven, how, and what has not.

The rule this file follows is the same one `scripts/verify-opsbridge.ps1` follows:
**a check that could not be run is reported as not run, never as a pass.** Nothing
below is reconstructed from memory or inferred from code that "should" work.

---

## Captured artifacts

There is currently **one** captured evidence file in the repository.

| File | Covers | Captured |
| --- | --- | --- |
| [`evidence/docker/build-and-run.md`](../evidence/docker/build-and-run.md) | The full container build and local runtime proof — real terminal output, ~300 lines | 2026-08-16, Windows 11 Pro 26200, Docker 29.1.3 |

### What that file actually proves

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

---

## Reproducible checks — verified, no artifact captured

These were run while writing this documentation. The output below is what the
commands actually printed on this machine; no file under `evidence/` records them
yet, so treat the command as the evidence and re-run it rather than citing this
page.

| Check | Command | Result |
| --- | --- | --- |
| Test suite (offline) | `python -m pytest -q` | `58 passed, 10 deselected in 1.82s` |
| Live tenant tests collect | `python -m pytest -m integration --collect-only -q` | `10/68 tests collected (58 deselected)` |
| Live tenant tests skip cleanly with no credentials | `python -m pytest -m integration -q` | `10 skipped, 58 deselected` — they have **not** been seen to pass; no credentials were present on this machine |
| Bicep template compiles | `bicep build infra/main.bicep --stdout` | Exit 0, **zero diagnostics**, 14,610-byte ARM template. Bicep CLI 0.46.1 |
| `.gitignore` was the first commit | `git show --stat $(git rev-list --max-parents=0 HEAD)` | `50f92b7` — one file changed, `.gitignore`, 19 insertions |
| No git remote exists | `git remote -v` | Empty — which is why no workflow has ever run |

Note the test count: `opsbridge-state.json` and the commit message on `fd43959`
both say 56. The suite is **57** and has been re-run to confirm; a test was added
during the container work (`test_metrics_is_503_when_configuration_is_missing`).
Where the state file and the suite disagree, the suite wins.

---

## Empty by design — nothing has been deployed

These directories exist and are **empty**. Each is waiting on something that
requires an account that does not exist yet.

| Directory | Would hold | Blocked on |
| --- | --- | --- |
| `evidence/azure/` | Deployment output, resource list, portal screenshots | No Azure subscription |
| `evidence/github-actions/` | A green pipeline run, job summaries | No GitHub remote — the repo has never been pushed |
| `evidence/graph/` | Real Graph responses, token acquisition | No Entra tenant, no admin consent |
| `evidence/sharepoint/` | The Assets list before and after a sync | No M365 tenant, no SharePoint site |
| `evidence/monitoring/` | Log Analytics queries, a fired alert | Requires deployed Log Analytics |
| `evidence/cost/` | A Cost Management export showing $0 | Requires a subscription with resources |
| `evidence/security/` | Role assignment listings, Key Vault access proof | Requires a deployed vault |
| `evidence/local/` | Local end-to-end sync against a real tenant | Requires credentials |
| `evidence/tests/` | A captured pytest transcript | Nothing blocking — simply not captured to a file yet |

---

## What is therefore **not** proven

Said plainly, so nobody has to infer it from an absence:

- **Nothing has run in Azure.** Not the job, not the API, not the Bicep template.
  `bicep build` proves the template compiles; it does not prove ARM accepts it,
  that the Key Vault reference resolves, that the role assignment propagates, or
  that ingress comes up. `az deployment group validate` and `--what-if` both need
  an authenticated subscription and have not been run.
- **Neither GitHub workflow has ever executed.** There is no remote, so there is no
  run to link to. The OIDC federation described in `SECURITY.md` and
  `DEPLOYMENT.md` is a design, unexercised.
- **No real Microsoft Graph call has ever been made.** Every test stubs MSAL and
  intercepts httpx. The client's paging, retry and error handling are verified
  against simulated responses — good tests, but not the real API. Graph's actual
  throttling behaviour, its real `Retry-After` values, and the exact shape of
  `physicalIds` on a real device object are unconfirmed.
- **The SharePoint provisioning script has never touched a real site.** Its
  `--dry-run` plan runs offline; the create path has not executed. In particular,
  whether `Sites.Selected` with the `write` role is sufficient to *create* lists
  and columns — as opposed to reading and patching items — is an expectation, not
  a finding.
- **`deploy-opsbridge.ps1` and `destroy-cloud.ps1` have never run against Azure.**
  `verify-opsbridge.ps1` does run end to end today, and reports every cloud check
  as SKIP.
- **No performance, cold-start, uptime, or throughput number exists anywhere in
  this repository.** Nothing has run long enough or in the right place to measure,
  and no such figure has been estimated and presented as measured. The cost figures
  in [`COST.md`](COST.md) are arithmetic over published pricing, explicitly labelled
  as such.

---

## How to regenerate everything that *can* be proven today

Offline, on any machine with Python 3.12 and Docker:

```bash
python -m pytest -q                                   # 58 passed, 10 deselected
bicep build infra/main.bicep --stdout > /dev/null      # 0 diagnostics
docker build -t opsbridge365:local .
docker run --rm opsbridge365:local id -u               # 10001
docker run -d --rm --name ev -p 8000:8000 opsbridge365:local
curl -s -i http://localhost:8000/healthz               # 200
curl -s -w '\nHTTP %{http_code}\n' http://localhost:8000/metrics   # 503
docker stop ev
```

Or all of it at once, with an honest SKIP for anything unavailable:

```powershell
powershell -NoProfile -File scripts/verify-opsbridge.ps1
```

Once the accounts in [`DEPLOYMENT.md`](DEPLOYMENT.md) Phase 1 exist, the empty
directories above are the checklist: deploy, then capture the deployment outputs,
the green pipeline run, a job execution log, the Assets list before and after, and
a Cost Management export. Capture them *before* the 30-day E5 trial expires — the
repository, the template and the captured evidence do not expire, but the tenant
does.
