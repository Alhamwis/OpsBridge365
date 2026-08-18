# Cost evidence — observed

**Observed spend to date: `$0.00`.**

Budget `opsbridge-monthly-20`: **$20/month**, scoped to `rg-opsbridge365`, with
alerts at **50% and 90% of actual** and **100% forecasted**. It was created
**before any billable resource existed** — a cost guardrail added after the
workload it guards has already had an unguarded window, and the forecast alert in
particular only helps if it predates the spending it is meant to predict.

---

## Why the deployed architecture draws nothing

| Resource | Why it bills $0 today |
| --- | --- |
| Log Analytics `opsbridge-logs` | PerGB2018 with **30-day retention** — inside the free 31-day window — and 5 GB/month of ingest is free. This workload emits kilobytes: a few lines per sync, a line per request |
| Key Vault (standard) | Per-operation pricing. Two workloads reading one secret at replica start is hundreds of operations a month |
| Container Apps environment | The environment itself is not billed; replica-seconds are |
| Container App `opsbridge-api` | **`minReplicas: 0`**, and the idle replica count was **observed at 0**. No replica, no bill |
| Container Apps Job `opsbridge-sync` | Four short runs a day at 0.25 vCPU / 0.5 GiB, against a monthly free grant of 180,000 vCPU-seconds and 360,000 GiB-seconds |
| Managed identity `opsbridge-id` | Managed identities are free |
| GHCR | Public package on a public repo — free. This is the Azure Container Registry line item that was designed out |
| GitHub Actions | Free for public repositories |

`minReplicas: 0` is the load-bearing one, and it is the only line in this table
with a *measured* consequence attached: the observed cold start from zero
replicas is **714 ms**, warm is **143 ms**. That 571 ms is what an idle month
costs instead of money.

## The honest caveat

**$0.00 partly reflects a subscription that is only hours old.** Azure bills on
usage that has accumulated, and almost none has. A number this clean, this early,
is weak evidence on its own — it is consistent with a well-designed architecture
and equally consistent with one that simply has not been running long enough to
show up.

What would make it strong evidence is a full billing cycle at this configuration,
and that has not happened.

**Expected steady state: `$0.00`/month**, on the assumptions that the API stays
at `minReplicas: 0`, traffic stays at demo volume, log volume stays inside the
5 GB free ingest, retention stays at 30 days, the GHCR package stays public, and
the subscription's Container Apps free grant is not consumed by another project.
Every one of those can stop being true.

## What would push it above zero

| Change | Effect |
| --- | --- |
| `minReplicas: 1` on the API | The big one. A single always-on replica at 0.25 vCPU is roughly 648,000 vCPU-seconds a month — about 3.6× the entire free grant — and bills from the first hour |
| A dashboard polling `/metrics` | Keeps a replica awake continuously. Same outcome as the line above, arrived at by accident rather than by decision |
| A much larger device fleet | The sync pages through all users and all devices on every run. More replica-seconds, more log volume, and Graph throttling with the retries that follow |
| Verbose logging, or a retry storm | Past 5 GB/month, Log Analytics bills per GB |
| Retention beyond 31 days | Billed per GB-month |
| Switching to ACR | Basic is a fixed monthly charge whether or not anything is pulled |
| Private endpoints / Key Vault firewall | Correct production hardening, and not free |

## Guardrails, all four now live

| # | Guardrail | State |
| --- | --- | --- |
| 1 | `replicaTimeout: 1800` on the sync job | ✅ Deployed — a hung Graph call is killed at 30 minutes |
| 2 | `replicaRetryLimit: 1` | ✅ Deployed — a failing sync retries once, then waits for the next cron tick |
| 3 | `maxReplicas: 1` on the API | ✅ Deployed — caps what a traffic spike can spend |
| 4 | Budget `opsbridge-monthly-20` | ✅ Live, and predates every resource above |

Three of these used to be "in the template, not deployed". They are now
configuration on running resources — see
[`../azure/deployment.md`](../azure/deployment.md).

The point of a $20 alert is not that it will fire. Expected spend is $0. It is
finding out from an email rather than from a statement.

Full model, assumptions and arithmetic: [`../../docs/COST.md`](../../docs/COST.md).
