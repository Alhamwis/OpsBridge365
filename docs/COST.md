# Cost

What this architecture is expected to cost, why, and what would break that.

> **Observed spend: $0.00, read 2026-08-29** — against a deployment that is
> actually running. Log Analytics, Key Vault, the Container Apps environment, the
> sync Job and the API all exist in `rg-opsbridge365`, the job has executed, and
> the API has served requests. The **idle replica count was observed at 0** on
> the same date, which is the mechanism the whole figure rests on. The command
> behind each of those checks is in [`STATUS.md`](STATUS.md).
>
> **The honest caveat: eleven days is not a billing cycle.** The subscription and
> the deployment were created 2026-08-18. Azure bills on accumulated usage, and
> not much has accumulated. A number this clean, this early, is consistent with a
> well-designed architecture and equally consistent with one that has not been
> running long enough to show up in a bill. What would settle it is a full
> billing cycle at this configuration, and that has not happened.
>
> So: the *architecture* is deployed and measured; the *cost* is deployed and
> thinly sampled. The per-line arithmetic below is still arithmetic over
> published pricing. Prices are list rates as of 2026-08, quoted for the shape of
> the bill; they change, and they vary by region.

**Expected steady state: $0.00/month**, under the assumptions listed below. Not
"free forever" — free *given these assumptions*, each of which can stop being
true.

---

## Where the money would go, line by line

| Resource | Pricing model | Expected | Why |
| --- | --- | --- | --- |
| Container Apps **Job** (`opsbridge-sync`) | Per vCPU-second and GiB-second of replica life | **$0** | Billed only while a replica is alive. See the math below — the monthly draw is well under 1% of the free grant |
| Container **App** (`opsbridge-api`) | Same, plus per-request | **$0** | `minReplicas: 0`, and the **idle replica count was observed at 0** (2026-08-29). No replica, no bill. An idle month is genuinely zero, not rounded to zero. The price is paid in latency instead: **20.2 s** cold from zero replicas, **248 ms** warm |
| Container Apps **environment** | — | **$0** | The environment itself is not billed. You pay for replica seconds |
| **Log Analytics** workspace | Per GB ingested (~$2.76/GB beyond the free tier) | **$0** | 5 GB/month ingestion is free. This workload emits kilobytes: a few log lines per sync, a request line per API call |
| Log Analytics **retention** | Per GB-month beyond 31 days | **$0** | `retentionInDays: 30` — deliberately inside the free 31-day window |
| **Key Vault** (standard) | ~$0.03 per 10,000 operations | **$0** | Secrets are read at replica start, not per request: the job reads `graph-client-secret`, the API reads that and `metrics-api-token`. Hundreds of operations a month against a 10,000-operation unit |
| **Managed identity** | — | **$0** | Managed identities are free |
| **GHCR** | — | **$0** | Free for public images on a public repository. This is the ACR line item that was designed out |
| **GitHub Actions** | Minutes | **$0** | Free for public repositories — including the scanning jobs added in this release |

Two of the eight resources in the group are not modelled above: the
`opsbridge-sync-failed` log-search alert rule and the `opsbridge-alerts` action
group. Those sit on Azure Monitor's own meters, which this file does not carry
arithmetic for. Both were deployed before the $0.00 reading above was taken.

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

## What this release changed about the cost model

Three of the changes shipped in this release move the model, and all three move
it the same way. None of them was made for cost reasons; cost is the side effect.

**The 45-second `/metrics` cache is the largest single reduction.** The endpoint
used to compute a fresh answer per request: every call minted an Entra token and
issued Graph reads. It is now served from a 45-second cache, and concurrent
misses are collapsed into one upstream fetch (single-flight), so a burst of *n*
callers inside one window costs one Graph read instead of *n*. A 30-request/minute
per-caller rate limit puts a ceiling on how fast anyone can drive the miss path
at all, and a 25-second wall-clock deadline bounds the whole operation.

Authentication does *not* stop a stranger waking the container — the 401 is
issued by the application, so ingress still starts a replica to produce it. What
it stops is the expensive half: an unauthenticated request does no Graph work and
mints no token. The public [`/demo/metrics`](https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io/demo/metrics)
endpoint is the same shape — it wakes the app and returns synthetic data with no
upstream call at all, which is why it is the safe thing to point an audience at.

**The health workflow probes every 4 hours precisely so it does not hold the app
awake.** `.github/workflows/health.yml` runs on a schedule and on manual
dispatch; it calls `/healthz`, checks `/demo/metrics` is synthetic, and checks
`/metrics` still refuses an unauthenticated caller. It never calls `/metrics`
with a token, so the scheduled probe does zero Graph work.

The cadence is a cost decision. Assume — and this is an assumption, not a
measurement; the scale-in cooldown on this app has not been timed — that one
probe keeps a replica alive for five minutes before it scales back to zero:

```
6 probes/day x 30 days x 300 s  = 54,000 replica-seconds
54,000 s x 0.25 vCPU            = 13,500 vCPU-seconds   (7.5% of 180,000)
54,000 s x 0.5 GiB              = 27,000 GiB-seconds    (7.5% of 360,000)
```

That makes the health check the largest consumer of replica seconds in the whole
design, and it is still an order of magnitude inside the grant. Run the same
probe every minute and the replica never scales in at all — which is assumption 2
below, arrived at by a monitoring decision rather than an infrastructure one. The
retry budget in that workflow (6 attempts, 20 s apart, 30 s timeout) exists
because a probe against a scaled-to-zero app has to absorb a ~20-second cold
start without going red.

The price of the 4-hour cadence is paid in what cannot be claimed: six samples a
day cannot support an uptime percentage, so none is published.

**The new supply-chain jobs cost minutes, not money.** CodeQL, the Trivy
filesystem and image scans, the SPDX SBOM step and dependency-review all run on
GitHub-hosted runners, and Actions minutes are free for public repositories.
CodeQL and dependency-review are free entitlements *for public repositories
specifically*. Dependabot is free everywhere. They add wall-clock time to CI —
and, because ruff is now a hard gate rather than advisory, a lint failure now
stops the build before it ever reaches the paid-in-seconds parts of Azure.

---

## One dated cost event: the M365 trial

Everything above is Azure. The Microsoft 365 side is a different tenant with its
own billing, and it is the one line item in this project that changes on a date
rather than on traffic.

The M365 subscription is a **trial** — `O365_BUSINESS_PREMIUM`, `isTrial: true`,
`nextLifecycleDateTime` **2026-09-16**. Earlier revisions of this repository
stated it was a paid Business Standard plan and concluded there was no clock.
There is a clock.

Two outcomes, and only one of them is free:

- **Turn recurring billing off before 2026-09-16.** The trial lapses. `/healthz`
  and `/demo/metrics` keep working, `/metrics` returns 502 — an honest failure
  rather than stale numbers presented as live — the sync job fails and the
  `opsbridge-sync-failed` alert rule catches it, and Azure is unaffected at
  $0.00, because it is a different tenant.
- **Do nothing.** The trial converts to a paid subscription by itself. That is a
  real recurring charge, per licence, and it is the only way this project starts
  costing money without anyone changing a line of infrastructure.

No figure is quoted here on purpose. Check the current per-licence price in the
M365 admin centre at the time you read this.

The $20 budget below cannot help with either outcome: it is scoped to the Azure
resource group and has no visibility into M365 billing at all. This one is a
diary entry, not a control.

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
   more or less permanently, which is assumption 2 by another route. The 45-second
   cache and the 30/minute limiter bound the *Graph* cost of that, not the
   replica-awake cost: a poller that gets a cached response still kept the
   container up to receive the request.
4. **Log volume stays small.** 5 GB/month is a lot of text, but a retry storm, a
   `DEBUG` log level in production, or a much larger device fleet all move real
   volume. The application logs at `INFO` and truncates Graph error bodies to 500
   characters, which is a deliberate part of this.
5. **The GHCR package stays public.** ✅ It is — `ghcr.io/alhamwis/opsbridge365`
   is public, checked 2026-08-29 — and Container Apps pulls it with no registry
   credential and no `registries:` block in the template. That was first proven
   by the deploy run of 2026-08-18 and has held on every deploy since; the
   [deploy run list](https://github.com/Alhamwis/OpsBridge365/actions/workflows/deploy.yml)
   is the current record. Make it private and Container Apps needs registry
   credentials — which means a registry secret in Key Vault, or ACR, which has no
   free tier.
6. **The repository stays public.** GHCR storage, Actions minutes, CodeQL and
   dependency-review are all free *because* it is public. Private changes every
   one of those, and CodeQL in particular becomes a paid feature.
7. **The device and ticket lists stay small.** The sync pages through *all* users
   and *all* devices on every run. At tens of devices that is seconds; at tens of
   thousands it is minutes of replica time per run, plus Graph throttling and the
   retries that follow.
8. **Nothing is deployed twice.** A second region, or `zoneRedundant: true`,
   multiplies everything. The template sets `zoneRedundant: false`.
9. **The Azure for Students credit is a safety margin, not the plan.** It is $100
   over 12 months. The architecture is designed to sit at $0 *without* touching
   it, so the credit absorbs mistakes rather than funding normal operation. When
   the student offer ends, a pay-as-you-go subscription still gets the same
   Container Apps free grant — that part does not depend on the student status.

---

## What would push it above zero

| Change | Effect |
| --- | --- |
| `minReplicas: 1` on the API | The single biggest one. An always-on replica exceeds the whole free grant several times over |
| Sustained polling of `/metrics` | Keeps a replica awake; same effect as above, arrived at accidentally. Now partly bounded — 30 requests/minute per caller, and a 45-second cache means polling faster than that buys the poller nothing — but a poller still holds the container up |
| Probing health far more often than every 4 hours | Same mechanism as polling `/metrics`, from the monitoring side. See the arithmetic above |
| Raising `maxReplicas` and getting real traffic | The cap exists precisely so a request flood cannot spend without a deliberate change |
| Verbose logging, or a much larger fleet | Past 5 GB/month, Log Analytics bills per GB |
| Retention beyond 31 days | Billed per GB-month |
| Switching to Azure Container Registry | Basic ACR is a fixed monthly charge whether or not anything is pulled — the one recurring cost this design removed |
| Making the repository or the GHCR package private | Moves registry storage, CI minutes and code scanning onto paid meters at GitHub, not at Azure |
| A dedicated workload profile instead of Consumption | Bills for provisioned capacity, not per-second usage |
| Private endpoints / Key Vault firewall | The correct production hardening, and not free |
| Significant data egress | Container Apps includes some free egress; large sustained transfer out is billed |
| **The M365 trial converting on 2026-09-16** | Not an Azure charge at all, which is exactly why the resource-group budget cannot see it |
| Leaving a soft-deleted Key Vault around | Costs nothing, but blocks recreating a vault with the same name for 7 days — a teardown gotcha, not a cost one |

---

## Guardrails

Four in the platform. Three of them are declared in `infra/main.bicep` — so a
rebuild restores them — and only the budget lives outside any template.

| # | Guardrail | State |
| --- | --- | --- |
| 1 | **`replicaTimeout: 1800`** on the sync job — a hung Graph call is killed at 30 minutes and cannot bill indefinitely | Declared in `infra/main.bicep`. Read back off `opsbridge-sync` **2026-08-29**: `az containerapp job show -g rg-opsbridge365 -n opsbridge-sync --query properties.configuration.replicaTimeout` |
| 2 | **`replicaRetryLimit: 1`** — a failing sync retries once and then waits for the next cron tick, instead of looping on a Graph outage and burning free-grant seconds | Declared in `infra/main.bicep`. Read back **2026-08-29** from the same command (`.replicaRetryLimit`) |
| 3 | **`maxReplicas: 1`** on the API — caps what traffic can spend | Declared in `infra/main.bicep` (cpu 0.25 / memory 0.5Gi). Re-check with `az containerapp show -g rg-opsbridge365 -n opsbridge-api --query properties.template.scale` |
| 4 | **Budget `opsbridge-monthly-20`** — $20/month, scoped to the resource group, alerting at **50% and 90% of actual** and **100% forecasted** | ✅ Live, and it predates every resource above. Spend against it read **$0.00** on 2026-08-29 |

Three more now sit in the application rather than the platform, and they close a
gap the template never could. Before this release, individual Graph requests were
bounded — a 30-second HTTP timeout, three attempts, a capped `Retry-After` — but
the *aggregate* operation behind `/metrics` was not: paging had no overall
deadline, so a pathological paging loop could have held a replica alive far
longer than any single timeout suggested, and any anonymous caller could start
one. The three are the **25-second wall-clock deadline** on the whole `/metrics`
operation, the **30-request/minute per-caller rate limit**, and the **45-second
cache**. The rationale for each is in [`SECURITY.md`](SECURITY.md); the cost
effect is that the worst case is now a bounded number of seconds instead of an
open-ended one.

The budget deliberately came *first*, before any resource that could spend money.
A cost guardrail created after the workload it guards has already had a window in
which it was not guarded — and the forecast alert in particular only helps if it
predates the spending it is meant to predict.

Note what each alert threshold buys, because three of them is not redundancy:
50% actual is "something is running that you did not expect," 90% actual is "act
now," and 100% *forecasted* fires on a trajectory rather than a total — it can
warn on day 4 of a month that the run rate ends above $20, which the actual-spend
alerts cannot do until it is nearly too late.

The point of a $20 alert is not that it will fire. Expected spend is $0. It is
knowing where the guardrail is, and finding out from an email rather than from a
statement.

**Teardown is a cost control too.** `scripts/destroy-cloud.ps1` deletes the entire
resource group, and `-PurgeKeyVault` also purges the soft-deleted vault so the
teardown is genuinely complete. It requires the resource group name typed back
exactly — there is no `-Force` shortcut — and `-WhatIf` lists what would go
without deleting anything. It has never been run against the live resource group,
so it is a design property rather than a demonstrated one. Note also that
`bootstrap.bicep` resources — the vault, the identity, the secret values — go
with it: recreating the deployment after a teardown means re-running the
bootstrap, not just the routine deploy.

---

## The honest summary

An idle month costs nothing, and that is a design outcome rather than luck: a
scheduled job that exits instead of an always-on scheduler, an API that scales to
zero instead of holding a replica, a free public registry instead of ACR, log
retention set inside the free window, and a health check paced so that watching
the system does not keep the system running. Every one of those was a decision
with a stated trade-off — cold starts, no in-process state, a public image, no
uptime percentage.

What it does *not* mean: it is not free at production scale, it is not free with a
polled dashboard, and **it has not been measured over time.** The $0.00 in Cost
Management is the cost of a *running* system rather than an empty resource group,
but as of 2026-08-29 it covers eleven days, not a billing cycle. A full month at
this configuration is the only thing that turns these numbers from arithmetic into
evidence, and the $20 budget is already in place to catch it if the arithmetic is
wrong.

The one number here that is genuinely measured rather than modelled is the cost
of scaling to zero, and it is not money at all: **20.2 s** on the first request
after the replica count was observed at 0, against **248 ms** warm, measured
2026-08-29. A second cold run gave 21.3 s. Roughly twenty seconds is the whole
bill.

> **Corrected 2026-08-29.** This file, and most of the repository, previously
> quoted **714 ms** cold and **143 ms** warm. That cold figure was wrong by about
> 30×, and it was wrong in a way that should have been caught by inspection: a
> Container Apps replica that starts from zero has to schedule, pull an image and
> start a Python process, and none of that happens in 714 ms. It was almost
> certainly measured against an app that was already awake. The re-measurement
> above was taken with the replica count read as 0 immediately beforehand, and
> repeated. The conclusion the old number supported — that scale-to-zero is paid
> for in latency, not money — survives; the size of the payment was understated
> by a factor of thirty, and the ~3-minute `/healthz` retry window in the deploy
> workflow turns out to be a necessity rather than a courtesy.
