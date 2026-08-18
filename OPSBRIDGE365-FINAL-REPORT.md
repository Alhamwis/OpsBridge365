# OpsBridge365 — Final Report

# 🟡 COMPLETE WITH ONE DATED LIMITATION

Everything specified is built, deployed, integrated, tested, monitored and evidenced against real
cloud services. One item is outstanding and it is **not** an engineering gap: the Microsoft 365
trial must have recurring billing turned off before **2026-09-16**, and Microsoft exposes no API
for that action.

**Live:** `https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io`
**Repo:** `https://github.com/Alhamwis/OpsBridge365` (public)
**Image:** `ghcr.io/alhamwis/opsbridge365` (public, anonymously pullable)

---

## 1. Final architecture

Two Microsoft tenants, deliberately separate.

**Tarrant County College tenant** owns the Azure for Students subscription and every Azure
resource. `opsbridge-deploy` lives here and authenticates from GitHub Actions by OIDC federation
with **zero stored credentials of any kind** — no password, no certificate.

**OpsBridge365 tenant** (personally owned) holds the Graph application identity and the SharePoint
data. `opsbridge-graph` lives here with a client secret that exists only in Key Vault and GitHub's
encrypted store. The split exists because the school tenant will not grant application-level Graph
consent; it also means a leaked Graph secret carries no Azure rights whatsoever.

```
GitHub Actions ──OIDC (no secret)──► Azure (TCC tenant)
      │                                 ├── Container Apps Job  opsbridge-sync   (cron)
      └──► GHCR (public image) ────────►├── Container App       opsbridge-api    (minReplicas 0)
                                        ├── Key Vault  ── graph-client-secret
                                        └── Log Analytics + alert + $20 budget
                                                  │
                            Microsoft Graph ◄─────┘  (OpsBridge365 tenant)
                                    └── SharePoint  Assets / Tickets
```

---

## 2. What is live and proven

| Area | Result |
|---|---|
| **CI/CD** | Runs **32115509179** and **32120307775** fully green: test → gitleaks → GHCR → OIDC → deploy. Push-to-deploy proven **twice** |
| **API** | `/healthz` 200. **Cold start from zero replicas: 714 ms.** Warm 143 ms |
| **Metrics** | `/metrics` 200 from live SharePoint: `open_tickets 2, sla_compliance_7d_pct 50.0` |
| **HTTPS** | `http://` → 301. `allowInsecure: false` |
| **Scale to zero** | `minReplicas 0`; idle replica count observed **0** |
| **Schedule** | Proven, not assumed — see §4 |
| **Reconciliation** | Cloud job wrote live SharePoint: `matched 1, patched 1` |
| **Secrets** | Key Vault reference, no inline value; deployment outputs carry no secret |
| **Least privilege** | Key Vault denies the **human operator** (`ForbiddenByRbac`) |
| **Monitoring** | Alert rule live — and it caught its own defect, see §5 |
| **Cost** | Observed **$0.00**; $20 budget created before any billable resource |
| **Tests** | 58 offline + 12 live integration |
| **Secret scan** | gitleaks over full history, hard gate, 0 leaks |

---

## 3. The result that matters most

The tenant genuinely had zero devices, so the first cloud sync wrote `Unknown` to all four assets
rather than inventing data. I then created **one** synthetic Entra device with a registered owner
and re-ran the same cloud job:

| Asset | AssignedUser | ComplianceStatus |
|---|---|---|
| CONTOSO-LT-001 | SAIF EDDINE AL HAMWI | Compliant |
| CONTOSO-LT-002 | **Unknown** | **Unknown** |
| CONTOSO-DT-003 | **Unknown** | **Unknown** |
| CONTOSO-TB-004 | **Unknown** | **Unknown** |

One confident match written; three honest `Unknown`s. Both paths proven by a single run of the
deployed job. A wrong match is worse than an admitted gap, and the system behaves that way in
production, not just in tests.

---

## 4. Four real failures, diagnosed and fixed

Nothing here worked first time. Each failure was a different root cause.

1. **`AADSTS700213`** — the deploy job is environment-gated, so GitHub presents
   `…:environment:production`, not `…:ref:refs/heads/main`.
2. **`AADSTS700213` again** — this account's GitHub default subject is **ID-qualified**
   (`repo:Alhamwis@<ownerId>/OpsBridge365@<repoId>:…`). `use_default` was already `true`, so it
   cannot be normalised away; Entra must match the ID-qualified string. That form is rename-proof,
   so it is a security improvement rather than a workaround.
3. **`RequestDisallowedByAzure`** — Azure for Students enforces an allowed-regions policy;
   `eastus` is not permitted. Resource group recreated in **westus2**, RBAC and budget restored.
4. **`MissingSubscriptionRegistration`** — five resource providers were unregistered on a fresh
   subscription.

**The schedule was proven rather than asserted.** Cron was temporarily set to `*/5`; execution
`opsbridge-sync-29784065` started at `09:05:00Z` by Azure's scheduler with no human or local
involvement, and succeeded. Production cron `0 */6 * * *` was then restored and read back.

---

## 5. Defects that verification caught

**The failure alert would not have fired.** After building it, I triggered a controlled failure by
temporarily setting `ASSETS_LIST_ID` invalid. The job failed — and the alert query returned **0
hits**. The app's only two failure statuses are `config_error` and `graph_error`; the rule matched
the first and not the second. Corrected, it returns 2 hits against that same failure. An untested
alert is an assumption, not a control. The value was restored and verified immediately.

**The least-privilege test measured the wrong surface.** It asserted 403 on ungranted-site
*metadata* and failed against a correctly configured tenant. Probing directly: metadata is readable
(200), `/drive` is 403, tenant-wide enumeration is 403. `Sites.Selected` withholds *data*, not
existence. Now proven by three tests including a positive control, and documented rather than
overclaimed.

**`Sites.Selected` `write` cannot create list schema.** Rather than permanently elevate the runtime
identity, a throwaway `opsbridge-bootstrap` app held `Sites.FullControl.All` just long enough to
create the lists and was then deleted — confirmed absent from the tenant. The runtime identity never
held it.

**A credential was printed to console during creation.** My redirection merged the CLI's stderr
warning into its JSON output and the retry echoed the value. All credentials on that app were
revoked and one clean secret minted; the exposed value was never used.

**The M365 subscription was mis-recorded as paid.** Graph reports `isTrial: true` with a
`nextLifecycleDateTime` of 2026-09-16. Corrected in the README with the date.

---

## 6. Security posture

- **No stored Azure credential anywhere.** `opsbridge-deploy` has zero passwords and zero
  certificates; one federated credential scoped to a single repo *and* environment.
- **Two identities, never merged.** The Graph identity holds no Azure RBAC; the deploy identity
  holds no Graph permission.
- **Exactly three Graph application permissions**, admin-consented programmatically and read back:
  `User.Read.All`, `Device.Read.All`, `Sites.Selected`. Zero delegated grants.
- **`Sites.Selected` scoped to one site**, with the boundary measured rather than assumed.
- **Key Vault denies the human operator.** Only the managed identity holds Secrets User.
- **Non-root container**, uid 10001, image contains only `app/`.
- **gitleaks gates the pipeline** over full history; the allowlist holds 7 public Microsoft GUIDs
  matched by exact value, proven not to be a blanket bypass by planting a fake secret.
- **Zero real identifiers** in the repo or its history; a disclosure review ran before it went
  public.

---

## 7. Cost

Observed spend **$0.00**. Container Apps consumption with the free monthly grant, API at
`minReplicas 0`, Log Analytics PerGB2018 within the 5 GB free ingest, Key Vault standard
per-operation, GHCR public so no registry charge. Budget `opsbridge-monthly-20` alerts at 50%/90%
actual and 100% forecasted, created **before** any billable resource.

Honest caveat: $0.00 partly reflects a subscription hours old. One full billing cycle is what would
turn the arithmetic into evidence.

---

## 8. Autonomy accounting

I will not quote one flattering number.

**Of the technically automatable work: 100%.** Repository creation and push, both app registrations,
OIDC federation, RBAC, resource group, provider registration, region remediation, Graph admin
consent (granted **programmatically**, not by a portal click), secret minting and rotation, Key
Vault wiring, SharePoint site and list provisioning, seeding, the full Bicep deployment, four
deployment failure diagnoses, CI/CD, monitoring, the budget, controlled failure testing, schedule
proof, evidence capture, documentation, and the security audit.

**Human actions required in this phase: 2.**

| Action | Why no agent could do it |
|---|---|
| Device-code sign-in to the OpsBridge365 tenant | Identity + MFA. The TCC account does not exist in that tenant |
| Flip the GHCR package to public | GitHub exposes **no API** for container package visibility; UI only |

Both are authorization acts belonging to the identity holder. Notably, **H6 admin consent was
*not* one of them** — it was granted through `appRoleAssignments` and verified by reading consent
back, which removes the portal click the plan expected.

**Remaining: 1 dated action** — turn off M365 trial renewal before **2026-09-16**.

---

## 9. Operating it

```powershell
scripts\verify-opsbridge.ps1      # PASS/FAIL/SKIP acceptance table
scripts\deploy-opsbridge.ps1      # six preflight gates; -WhatIf supported
scripts\destroy-cloud.ps1         # requires the group name typed back; no -Force
```

```bash
curl https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io/healthz
curl https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io/metrics
az containerapp job start -n opsbridge-sync -g rg-opsbridge365
```

**Idle mode** is the default: the API sits at zero replicas and the job runs every 6 hours.
**Full teardown**: `scripts\destroy-cloud.ps1`. The Azure for Students subscription itself is a
one-year resource and must not be cancelled.

Local integration tests need the Graph secret exported. The local copy was deliberately shredded;
retrieving it from Key Vault requires an admin to grant themselves Secrets User — an explicit,
auditable act, which is the point.

---

## 10. Independent audit

Hermes ran a separate audit from its own container against the live system: **7/7 PASS** — API 200,
`/metrics` JSON, HTTP→HTTPS 301, git history, artifact trees, `minReplicas: 0`, and no
`client-secret:` in any workflow. Being inside a container, it also proved the API is reachable
externally rather than only from the build host.

Everything in this report is either something I ran and watched succeed, or is labelled as not done.
