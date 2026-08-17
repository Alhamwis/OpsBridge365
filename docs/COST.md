# Cost

What this architecture is expected to cost, why, and what would break that.

> **No bill has ever been observed.** Nothing is deployed — there is no Azure
> subscription yet. Everything below is derived from the resource configuration in
> `infra/main.bicep` and published Azure pricing, not from a Cost Management
> export. `evidence/cost/` is empty and will stay empty until something runs.
> Prices are list rates at time of writing, quoted for the *shape* of the bill;
> they change, and they vary by region.

**Expected steady state: $0.00/month**, under the assumptions listed below. Not
"free forever" — free *given these assumptions*, each of which can stop being
true.

---

## Where the money would go, line by line

| Resource | Pricing model | Expected | Why |
| --- | --- | --- | --- |
| Container Apps **Job** (`opsbridge-sync`) | Per vCPU-second and GiB-second of replica life | **$0** | Billed only while a replica is alive. See the math below — the monthly draw is well under 1% of the free grant |
| Container **App** (`opsbridge-api`) | Same, plus per-request | **$0** | `minReplicas: 0`. No replica, no bill. An idle month is genuinely zero, not rounded to zero |
| Container Apps **environment** | — | **$0** | The environment itself is not billed. You pay for replica seconds |
| **Log Analytics** workspace | Per GB ingested (~$2.76/GB beyond the free tier) | **$0** | 5 GB/month ingestion is free. This workload emits kilobytes: a few log lines per sync, a request line per API call |
| Log Analytics **retention** | Per GB-month beyond 31 days | **$0** | `retentionInDays: 30` — deliberately inside the free 31-day window |
| **Key Vault** (standard) | ~$0.03 per 10,000 operations | **$0** | Two workloads reading one secret at replica start. Hundreds of operations a month against a 10,000-operation unit |
| **Managed identity** | — | **$0** | Managed identities are free |
| **GHCR** | — | **$0** | Free for public images on a public repository. This is the ACR line item that was designed out |
| **GitHub Actions** | Minutes | **$0** | Free for public repositories |

### The free grant, and the actual draw

Azure Container Apps includes, **per subscription per month**:

- 180,000 vCPU-seconds
- 360,000 GiB-seconds
- 2 million requests

The sync job at its configured cron (`0 */6 * * *` — four runs a day), at
0.25 vCPU / 0.5 GiB, assuming a generous 30 seconds per run:

```
4 runs/day x 30 days            = 120 runs/month
120 runs x 30 s                 = 3,600 replica-seconds
3,600 s x 0.25 vCPU             = 900 vCPU-seconds      (0.5% of 180,000)
3,600 s x 0.5 GiB               = 1,800 GiB-seconds     (0.5% of 360,000)
```

The API adds only what its cold starts and request handling consume. At demo
volumes — a few dozen requests a month, a few seconds awake each time — that is
another rounding error. Even at 100× the assumed sync duration the job would sit
inside the grant.

> **Corrected 2026-08-16.** `infra/README.md` previously stated this as
> "~30 vCPU-seconds/month" — wrong by roughly the number of runs in a month (it
> costed a single run, not a month of them). Both figures are far inside the
> grant, so the conclusion never changed, but the arithmetic in that file now
> matches the working above.

---

## Assumptions this $0 depends on

State them, because "free" without assumptions is a sales claim.

1. **The subscription's Container Apps free grant is unconsumed by anything else.**
   It is per subscription, not per app. A second project in the same subscription
   eats the same 180,000 vCPU-seconds.
2. **The API stays at `minReplicas: 0`.** One line changes this. A single
   always-on replica at 0.25 vCPU consumes roughly 648,000 vCPU-seconds a month —
   about 3.6× the entire free grant — and starts billing immediately.
3. **Traffic stays at demo volume.** Scale-to-zero is free because nobody is
   asking. A dashboard polling `/metrics` every 30 seconds keeps a replica awake
   more or less permanently, which is assumption 2 by another route.
4. **Log volume stays small.** 5 GB/month is a lot of text, but a retry storm, a
   `DEBUG` log level in production, or a much larger device fleet all move real
   volume. The application logs at `INFO` and truncates Graph error bodies to 500
   characters, which is a deliberate part of this.
5. **The GHCR package stays public.** A private package would need pull
   credentials, which means a registry secret in Key Vault — or ACR, which has no
   free tier.
6. **The device and ticket lists stay small.** The sync pages through *all* users
   and *all* devices on every run. At tens of devices that is seconds; at tens of
   thousands it is minutes of replica time per run, plus Graph throttling and the
   retries that follow.
7. **Nothing is deployed twice.** A second region, or `zoneRedundant: true`,
   multiplies everything. The template sets `zoneRedundant: false`.
8. **The Azure for Students credit is a safety margin, not the plan.** It is $100
   over 12 months. The architecture is designed to sit at $0 *without* touching
   it, so the credit absorbs mistakes rather than funding normal operation. When
   the student offer ends, a pay-as-you-go subscription still gets the same
   Container Apps free grant — that part does not depend on the student status.

---

## What would push it above zero

| Change | Effect |
| --- | --- |
| `minReplicas: 1` on the API | The single biggest one. An always-on replica exceeds the whole free grant several times over |
| Sustained polling of `/metrics` | Keeps a replica awake; same effect as above, arrived at accidentally |
| Raising `maxReplicas` and getting real traffic | The cap exists precisely so a request flood cannot spend without a deliberate change |
| Verbose logging, or a much larger fleet | Past 5 GB/month, Log Analytics bills per GB |
| Retention beyond 31 days | Billed per GB-month |
| Switching to Azure Container Registry | Basic ACR is a fixed monthly charge whether or not anything is pulled — the one recurring cost this design removed |
| A dedicated workload profile instead of Consumption | Bills for provisioned capacity, not per-second usage |
| Private endpoints / Key Vault firewall | The correct production hardening, and not free |
| Significant data egress | Container Apps includes some free egress; large sustained transfer out is billed |
| Leaving a soft-deleted Key Vault around | Costs nothing, but blocks recreating a vault with the same name for 7 days — a teardown gotcha, not a cost one |

---

## Guardrails

Three exist in the code, and one is a manual step:

1. **`replicaTimeout: 1800`** on the sync job — a hung Graph call is killed at 30
   minutes and cannot bill indefinitely.
2. **`replicaRetryLimit: 1`** — a failing sync retries once and then waits for the
   next cron tick, instead of looping on a Graph outage and burning free-grant
   seconds.
3. **`maxReplicas: 1`** on the API — caps what traffic can spend.
4. **A budget alert**, which has to be set by hand once a subscription exists:

```powershell
az consumption budget create --budget-name opsbridge-guard --amount 20 `
  --time-grain Monthly --category Cost
```

Expected spend is $0. The point of a $20 alert is not that it will fire — it is
knowing where the guardrail is, and finding out from an email rather than from a
statement.

**Teardown is a cost control too.** `scripts/destroy-cloud.ps1` deletes the entire
resource group, and `-PurgeKeyVault` also purges the soft-deleted vault so the
teardown is genuinely complete. It requires the resource group name typed back
exactly — there is no `-Force` shortcut — and `-WhatIf` lists what would go
without deleting anything.

---

## The honest summary

An idle month costs nothing, and that is a design outcome rather than luck: a
scheduled job that exits instead of an always-on scheduler, an API that scales to
zero instead of holding a replica, a free public registry instead of ACR, and log
retention set inside the free window. Every one of those was a decision with a
stated trade-off — cold starts, no in-process state, a public image.

What it does *not* mean: it is not free at production scale, it is not free with a
polled dashboard, and it has not been measured. The first real bill is the only
thing that would turn these numbers from arithmetic into evidence.
