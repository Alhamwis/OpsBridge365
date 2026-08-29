# Status

What is true about the deployed system, when it was last checked, and the exact
command that checked it.

Two rules govern this file:

1. **A claim without a date is not a claim.** Every row carries the date it was
   measured. Anything not re-measured moves to *Historical* rather than quietly
   staying in *Current*.
2. **The command is part of the claim.** If you cannot re-run it, it is an
   assertion, not evidence.

---

## Current — verified 2026-08-29

| What | Result | Command |
| --- | --- | --- |
| API liveness | `200 {"status":"ok","version":"0.1.0"}` | `curl -s $BASE/healthz` |
| Cold start from zero replicas | **20.2 s** | replica count observed at 0, then `curl -w '%{time_total}' $BASE/healthz` |
| Warm request | **248 ms** | immediate second `curl` |
| Public demo endpoint | `200`, `"synthetic": true` | `curl -s $BASE/demo/metrics` |
| `/metrics` without a token | **401** | `curl -o /dev/null -w '%{http_code}' $BASE/metrics` |
| `/metrics` with a token | `200`, live SharePoint values | `curl -H "Authorization: Bearer $METRICS_API_TOKEN" $BASE/metrics` |
| Idle replica count | **0** | `az containerapp replica list -g rg-opsbridge365 -n opsbridge-api --revision <rev>` |
| Sync job schedule | `Schedule`, cron `0 */6 * * *`, retry 1, timeout 1800 | `az containerapp job show -g rg-opsbridge365 -n opsbridge-sync` |
| Azure resources | 8 in `rg-opsbridge365` (westus2) | `az resource list -g rg-opsbridge365 -o table` |
| Key Vault denies the human operator | `ForbiddenByRbac` | `az keyvault secret list --vault-name <vault>` |
| Deploy identity privilege | **Contributor only** (RBAC Administrator removed) | `az rest --method get --url ".../roleAssignments?api-version=2022-04-01"`, filtered to the deploy principal |
| Deploy identity cannot grant roles | Refused with `AuthorizationFailed` | asserted on every deployment by the `Verify - deployment identity CANNOT create role assignments` step |
| Offline tests | **106 passed, 12 deselected** | `python -m pytest -q` |
| Lint | clean, and a hard CI gate | `ruff check .` |
| Secret scan, full history | **0 findings** over 13 commits | `gitleaks git . --log-opts="--all"` — CI runs the same command; until 2026-08-29 it ran `gitleaks-action`, which scanned only the push range. See [SECURITY.md](SECURITY.md) |
| Same scan with the allowlist pointed away | **2 findings, both public Microsoft role GUIDs** | `gitleaks git . --log-opts="--all" -c /tmp/no-allowlist.toml` — see [SECURITY.md](SECURITY.md). Omitting `--config` does *not* disable the allowlist; gitleaks still reads `./.gitleaks.toml` |
| Bicep templates compile | 0 diagnostics | `az bicep build --file infra/main.bicep` and `.../bootstrap.bicep` |

### Subscription state

| | |
| --- | --- |
| Azure | "Azure for Students" on a college-managed tenant. Resource group `rg-opsbridge365`, **westus2**. Observed spend $0.00 |
| Microsoft 365 | **`O365_BUSINESS_PREMIUM` trial**, `isTrial: true`, `nextLifecycleDateTime` **2026-09-16** |

The M365 subscription is a **trial**, not a paid plan. Earlier revisions of this
repository stated the opposite and drew a conclusion from it ("no clock, so the
evidence does not need capturing before a deadline"). That conclusion was wrong.
The clock is real and the date is above.

### What happens if the trial lapses

| Surface | Behaviour |
| --- | --- |
| `GET /healthz` | Unaffected. No tenant dependency |
| `GET /demo/metrics` | Unaffected. Synthetic data, no upstream call |
| `GET /metrics` | Returns **502** — Graph auth or request failure, surfaced honestly. It does not serve stale numbers as if they were live |
| `opsbridge-sync` job | Fails and is caught by the `opsbridge-sync-failed` alert rule |
| Azure resources | Unaffected. Different tenant, still $0.00 |
| This repository | The historical evidence stays valid and stays labelled historical. No README claim depends on the trial still being alive |

---

## Historical evidence — captured 2026-08-18, not re-checked since

These were real observations. They are not claims about today.

| What was proven | Where |
| --- | --- |
| The cloud job read Graph and wrote the live Assets list: `users_fetched 1, devices_fetched 1, assets_fetched 4, matched 1, patched 1, unknown_last_check_in 1` | [`../evidence/sharepoint/reconciliation.md`](../evidence/sharepoint/reconciliation.md) |
| The schedule fires without human involvement: cron temporarily `*/5`, an execution started at `09:05:00Z` by Azure's scheduler and succeeded; production cron restored and read back | [`../evidence/azure/deployment.md`](../evidence/azure/deployment.md) |
| 12 live integration tests passed against the real tenant | [`../evidence/graph/live-integration-run.md`](../evidence/graph/live-integration-run.md) |
| `Sites.Selected` boundary probed with the app's own token | [`../evidence/security/posture.md`](../evidence/security/posture.md) |
| Alert rule tested by a controlled failure — and the first version of the query did not fire | [`../evidence/monitoring/alerting.md`](../evidence/monitoring/alerting.md) |
| Observed spend $0.00 against a running deployment | [`../evidence/cost/observed.md`](../evidence/cost/observed.md) |
| Image built, pushed and pulled with no registry credential | [`../evidence/docker/build-and-run.md`](../evidence/docker/build-and-run.md) |

---

## Findings worth keeping

Things this deployment established that are not obvious from the code, and that
cost real time to discover. Kept here because they are the kind of thing that is
rediscovered expensively.

**The secret scan was scanning one commit, not the history.** `ci.yml` and
`deploy.yml` checked out with `fetch-depth: 0` and the documentation claimed
full-history scanning on the strength of it. `gitleaks/gitleaks-action` scans the
push range instead: the run cited as proof logged `git log -p -U0 -1` and
`1 commits scanned`. The history was fetched and never read. It only surfaced
because a force-push left the action diffing from a commit that no longer
existed, and it failed the job having scanned ~0 bytes. Reading the workflow file
would never have caught it — the file was not the thing that was wrong. The job
now runs the gitleaks binary directly with `--log-opts="--all"`, pinned by
version and verified by checksum, and prints the number of commits it scanned.

**The alert query matched a status the app never emitted on that path.** The
failure alert was built, looked correct, was syntactically valid and pointed at
the right workspace. A controlled failure proved it returned **0 hits against a
genuinely failed job**: it matched `config_error`, while `app/sync.py`'s other
failure status is `graph_error` and that was what the failure emitted. Inspection
would never have caught it. An untested alert is an assumption, not a control.

**GitHub's OIDC subject can be ID-qualified.** The federated credential was first
created for `repo:<owner>/<repo>:ref:refs/heads/main` and failed with
`AADSTS700213`, because an environment-gated job presents
`...:environment:production` instead. Corrected, it failed again — this account's
default subject carries numeric owner and repository ids
(`repo:<owner>@<ownerId>/<repo>@<repoId>:environment:production`). `use_default`
was already `true`, so there was nothing to normalise; Entra has to match the
ID-qualified string. Kept rather than worked around: that form survives a
repository rename, which the name-based form does not.

**Azure for Students enforces an allowed-regions policy.** `eastus` is refused
with `RequestDisallowedByAzure`. Permitted at the time: `northcentralus`,
`mexicocentral`, `westus2`, `westus`, `canadacentral`. The resource group was
recreated in `westus2`; `main.bicep` needed no change because it takes location
from `resourceGroup().location`.

**A fresh subscription has resource providers unregistered.**
`Microsoft.App`, `Microsoft.KeyVault`, `Microsoft.OperationalInsights`,
`Microsoft.ManagedIdentity` and `Microsoft.Insights` all had to be registered.
ARM names one provider per failure, so this arrives as a sequence of failures
rather than one useful error.

**`Sites.Selected` withholds data, not existence.** With role `write` on one
site: ungranted-site *data* is 403, tenant-wide enumeration is 403, but an
ungranted site's *metadata* returns 200. That is expected Microsoft behaviour. An
integration test that asserted 403 on metadata failed against a correctly
configured tenant — the security property was real, the assertion was aimed at
the wrong surface.

**`Sites.Selected` with role `write` cannot create list schema.** Rather than
permanently widening the runtime identity, a throwaway app registration held
`Sites.FullControl.All` just long enough to create the two lists, and was then
deleted and confirmed absent. The runtime identity never held it.

**`az bicep build` proves the template, not the subscription.** It returned zero
diagnostics before each of the deployment failures above. Every one of them lived
between a valid template and one specific real subscription.

**A misconfigured redirection can print a credential.** During app-registration
setup, merging the CLI's stderr into its JSON output caused a retry to echo a
client secret to the console. Every credential on that app was revoked and one
clean secret minted. The lesson is in the tooling: `scripts/` never merges stderr
into a stream it then prints, and `deploy.yml` passes no secret on a command line.

---

## Not measured

Stated so the tables above are not read as a complete picture.

- **No uptime percentage.** The health workflow probes every 4 hours. Six samples
  a day cannot support a percentage, and publishing one would be inventing
  precision.
- **No load or throughput characterisation.** Cold and warm latency are two data
  points, not a performance profile.
- **Cost over a full billing cycle.** $0.00 was observed on a subscription hours
  old.
- **Fleet scale.** The end-to-end run matched one device for one user.
- **Teardown.** `destroy-cloud.ps1` has never been run against the live resource
  group, so "the blast radius is one resource group" remains a design property
  rather than a demonstrated one.
