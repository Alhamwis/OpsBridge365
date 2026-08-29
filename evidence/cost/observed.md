# Cost evidence — observed

> **HISTORICAL EVIDENCE — captured 2026-08-18.** Every figure here is as of that
> date. Azure spend has **not** been re-observed since, so nothing below is a
> claim about what the subscription costs today. For current state, see
> [`../../docs/STATUS.md`](../../docs/STATUS.md).

**Observed spend as of 2026-08-18: `$0.00`.**

Budget `opsbridge-monthly-20`: **$20/month**, scoped to `rg-opsbridge365`, with
alerts at **50% and 90% of actual** and **100% forecasted**. It was created
**before any billable resource existed** — a cost guardrail added after the
workload it guards has already had an unguarded window, and the forecast alert in
particular only helps if it predates the spending it is meant to predict.

---

## Why the deployed architecture draws nothing

| Resource | Why it billed $0 on the capture date |
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
with a *measured* consequence attached: an idle app pays a cold start on the
first request after it scales in.

That consequence was recorded here on 2026-08-18 as **714 ms** cold against
143 ms warm. **That figure was wrong.** Re-measured on **2026-08-29**, with the
replica count observed at 0 immediately beforehand so the scale-from-zero was
genuine: **20.2 s** cold, **248 ms** warm (a second run gave 21.3 s). So the
price of paying nothing while idle is a first request of roughly twenty seconds,
not roughly half a second. It is still the right trade for a demo-volume
workload, and it is a much larger one than this file originally claimed. See
[`../azure/deployment.md`](../azure/deployment.md) for the measurement.

## The honest caveat

**On 2026-08-18, `$0.00` reflected a subscription that was hours old.** Azure
bills on usage that has accumulated, and almost none had. A number this clean,
this early, is weak evidence on its own — it is consistent with a well-designed
architecture and equally consistent with one that simply has not been running
long enough to show up.

The subscription is no longer hours old. It is also not re-measured: nobody has
run `az consumption usage list` against it since the capture, so the correct
statement today is "$0.00 was observed on 2026-08-18 and has not been checked
since", not "$0.00 to date". The check is cheap; re-run it before quoting a
number.

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
| A dashboard polling `/metrics` | Keeps a replica awake continuously. Same outcome as the line above, arrived at by accident rather than by decision. The 45-second response cache added since this capture bounds the *upstream* Graph traffic, not the replica-seconds — a poller still holds the container awake |
| A much larger device fleet | The sync pages through all users and all devices on every run. More replica-seconds, more log volume, and Graph throttling with the retries that follow |
| Verbose logging, or a retry storm | Past 5 GB/month, Log Analytics bills per GB |
| Retention beyond 31 days | Billed per GB-month |
| Switching to ACR | Basic is a fixed monthly charge whether or not anything is pulled |
| Private endpoints / Key Vault firewall | Correct production hardening, and not free |

## Guardrails, all four live on the capture date

| # | Guardrail | State on 2026-08-18 |
| --- | --- | --- |
| 1 | `replicaTimeout: 1800` on the sync job | ✅ Deployed — a hung Graph call is killed at 30 minutes |
| 2 | `replicaRetryLimit: 1` | ✅ Deployed — a failing sync retries once, then waits for the next cron tick |
| 3 | `maxReplicas: 1` on the API | ✅ Deployed — caps what a traffic spike can spend |
| 4 | Budget `opsbridge-monthly-20` | ✅ Live, and predates every resource above |

Three of these had been "in the template, not deployed". As of this capture they
were configuration on running resources — see
[`../azure/deployment.md`](../azure/deployment.md).

A fifth arrived after this capture: `/metrics` is authenticated and rate limited
at 30 requests/minute per caller, and served from a 45-second cache. Be precise
about what that does and does not fix. It does **not** stop an anonymous loop
waking the container — the 401 is returned by the application process, so a
replica still has to start to refuse the request, and the note above about a
poller holding the container awake still stands. What it removes is the
*Microsoft Graph* amplification: an unauthenticated caller can no longer cause a
single upstream call, and an authenticated one causes at most one per 45 seconds
however fast it polls. Ingress-level blocking, not application-level auth, is
what would stop the wake-up itself.

The point of a $20 alert is not that it will fire. Expected spend is $0. It is
finding out from an email rather than from a statement.

Full model, assumptions and arithmetic: [`../../docs/COST.md`](../../docs/COST.md).
