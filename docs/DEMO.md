# Demo script — 5 minutes

A run sheet for walking an interviewer through OpsBridge365. **It is deployed and
running**, so this is the live demo. Every command below has been run against the
real thing.

```
BASE=https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io
```

The API is public and needs no credentials, so the first four minutes work from
any machine with `curl` — including the interviewer's.

---

## Before you start

```bash
git pull
export BASE=https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io
curl -s "$BASE/healthz"                  # confirm it is up
python -m pytest -q                      # confirm green: 58 passed, 12 deselected
```

Then **leave it alone for a few minutes.** `minReplicas: 0` means the replica
shuts down when idle, and the cold start is the demo. Waking it right before you
present throws away the best thirty seconds you have.

Have open, in this order: a terminal, `README.md` scrolled to the status table,
and the GitHub Actions run `32115509179`. Close everything else.

**The first fifteen seconds**, because it sets the frame:

> "This is the cloud half of an IT service desk tool — a scheduled job that
> reconciles device data from Microsoft Graph into a SharePoint asset list, and an
> API that reports live SLA numbers. It's deployed, it's running in Azure right
> now, and the pipeline that put it there uses no stored Azure credential. I'll
> show you the API first, then the parts that didn't work first time — those are
> the interesting bit."

---

## 0:00 — The problem (30 seconds)

No screen. Just say it:

> "A service desk has two chores nobody does. The asset register drifts from what
> the directory actually knows — who has which laptop, is it compliant, when did
> it last check in. And 'how many tickets are about to breach SLA' gets answered
> by opening a list and counting. This automates both."

## 0:30 — The live API, cold (60 seconds)

Hand them the URL if they want to run it themselves. Otherwise:

```bash
time curl -s "$BASE/healthz"
```

```json
{"status":"ok","version":"0.1.0"}
```

> "That pause was about seven tenths of a second — 714 milliseconds measured.
> There was no replica running a moment ago. That's `minReplicas: 0`, and it's
> why an idle month costs nothing rather than nearly nothing."

Immediately again:

```bash
time curl -s "$BASE/healthz"
```

> "143 milliseconds warm. The 571 millisecond difference is the entire price of
> scaling to zero."

Now the endpoint that does real work:

```bash
curl -s "$BASE/metrics"
```

```json
{"open_tickets":2,"due_within_30min":0,"sla_compliance_7d_pct":50.0,"resolved_last_7d":2,"sla_measured_last_7d":2}
```

> "That's read live from SharePoint through Microsoft Graph. And notice the last
> two fields — the percentage ships with its own sample size. Fifty percent of
> two. A percentage without its denominator is a half-truth, and this one refuses
> to be quoted out of context."

If they ask what happens with no data, open `app/metrics.py`:

> "Zero resolutions in the window returns `null`, not 0% and not 100%. And a
> ticket resolved with no due date counts as resolved but is excluded from the
> denominator, rather than assumed to have met its target. A metric that lies
> confidently is worse than no metric."

One more, because it takes four seconds:

```bash
curl -s -o /dev/null -w '%{http_code}\n' "${BASE/https/http}/healthz"    # 301
```

> "Plain HTTP redirects. `allowInsecure: false` on the ingress."

## 1:30 — The end-to-end proof (90 seconds)

**This is the headline. Do not rush it.**

> "The sync job runs on a cron in Azure — `0 */6 * * *`, a Container Apps *Job*,
> so it starts, runs, exits, and bills nothing in between. Here's what happened
> the first time it ran against the real tenant."

Show the table (it is in
[`evidence/sharepoint/reconciliation.md`](../evidence/sharepoint/reconciliation.md)),
or the live SharePoint list if you have it open:

| Asset | AssignedUser | ComplianceStatus |
| --- | --- | --- |
| `CONTOSO-LT-001` | SAIF EDDINE AL HAMWI | Compliant |
| `CONTOSO-LT-002` | Unknown | Unknown |
| `CONTOSO-DT-003` | Unknown | Unknown |
| `CONTOSO-TB-004` | Unknown | Unknown |

> "The tenant genuinely had zero devices, so the first cloud run had nothing to
> match and everything stayed `Unknown` — it did not invent data to look busy. So
> I created **one** device in Entra with a registered owner and ran the job
> again. One row got a real user and a real compliance status. Three stayed
> `Unknown`. Both behaviours proven by one run."

The job's own summary, captured by Log Analytics:

```
users_fetched: 1, devices_fetched: 1, assets_fetched: 4, matched: 1, patched: 1,
unknown_last_check_in: 1
```

> "That last field is the rule at its finest grain. `LastCheckIn` is a *date*
> column, and there is no date that means 'we don't know' — so the field is left
> untouched rather than stamped with an invented time, and the count is reported
> so the gap is visible instead of silent. Two other rules go with it: a device
> matches on serial number first and device name second, and **a key that matches
> two rows matches nothing**. A wrong value in an asset register is worse than an
> admitted gap, because nobody audits a field that looks filled in."

If you want to run it live — it takes about a minute, so only if you have the
time:

```bash
az containerapp job start -g rg-opsbridge365 -n opsbridge-sync
az containerapp job execution list -g rg-opsbridge365 -n opsbridge-sync -o table
```

## 3:00 — Security, in two commands' worth of talking (60 seconds)

Open the Actions run `32115509179` — four green jobs.

> "Push to main runs tests and a gitleaks scan over the *full history*, both hard
> gates. Only then does it build and push the image, and only then does it deploy.
> The deploy authenticates to Azure with OIDC federation — GitHub mints a
> short-lived token, Azure exchanges it, the token dies with the job. There is no
> Azure password anywhere in this repository or in its secrets."

Then the one that lands hardest:

> "There are two app registrations in two tenants. The one that can deploy has no
> credential at all — zero passwords, zero certificates. The one with a secret has
> no Azure permissions whatsoever. So the identity with authority has nothing to
> leak, and the credential that can leak has no authority.
>
> That secret lives in Key Vault, and the container gets a Key Vault *reference*,
> not a value — I can show you the app definition; it has a vault URL and no
> plaintext. And when **I** try to read that secret as myself, with Contributor on
> the resource group, Key Vault says `ForbiddenByRbac`. Only the managed identity
> can read it. That's the demonstration — least privilege that refuses *me*."

## 4:00 — The parts that didn't work (60 seconds)

**Volunteer this. It is the most credible minute in the demo.**

> "The deploy didn't work first time. Three runs failed for three different
> reasons, and none of them was a code problem — `bicep build` was clean through
> all of it.
>
> First: `AADSTS700213` on the OIDC login. The deploy job is gated on a GitHub
> environment, and an environment-gated job presents a subject of
> `environment:production`, not `ref:refs/heads/main`. My credential matched the
> branch. Second: same error again, because this account's subject is
> *ID-qualified* — the owner and repo carry numeric id suffixes, and
> `use_default` was already true, so there was nothing to normalise. Entra has to
> match that exact string. That form is rename-proof, so it's actually the better
> configuration. Third: `RequestDisallowedByAzure` — Azure for Students has an
> allowed-regions policy and `eastus` isn't on it. And on top of that, five
> resource providers were unregistered on the fresh subscription."

Then the one to finish on:

> "The best thing I found, I found by testing a control instead of trusting it. I
> built a Log Analytics alert for sync failures, then broke the job on purpose —
> pointed it at an invalid list id — to check the alert fired. **It didn't.** The
> app has two failure statuses, `config_error` and `graph_error`; my query matched
> the first and the real failure emitted the second. Zero hits against a job that
> genuinely failed. The fixed query returns two hits against that same failure.
>
> Nothing about that rule looked wrong. It was valid, it pointed at the right
> workspace, it matched a status the app really emits. Reading it would never have
> caught it. An untested alert is an assumption, not a control."

## 5:00 — Where it stands (30 seconds)

`README.md`, the status table, then the gaps immediately below it.

> "Everything in that table was measured, not estimated. And the gaps are stated
> right underneath: the $0.00 spend is real but the subscription is hours old, so
> that's hours of evidence and not a billing cycle. The end-to-end run was one
> user and one device. There's no load or uptime number. `/metrics` is
> unauthenticated, which is fine for a demo and not for production. I'd rather
> tell you that than have you find it."

---

## Fallbacks

| If this fails | Do this |
| --- | --- |
| **The live API doesn't respond** | Say so and switch: *"that's the cold start not co-operating."* The captured responses are in [`evidence/azure/deployment.md`](../evidence/azure/deployment.md), and the local container demo below runs with no network at all |
| **No network** | `docker build -t opsbridge365:local . && docker run --rm -p 8000:8000 opsbridge365:local`, then `curl localhost:8000/healthz` → 200 with **zero credentials**, and `/metrics` → 503 that names no variable to the caller. `docker run --rm opsbridge365:local id -u` → `10001` |
| **Docker is down too** | `pytest -q`, then walk the code: `metrics.py` for the SLA logic, `sync.py` for the ambiguity rule, `main.py` for the error handling. [`evidence/docker/build-and-run.md`](../evidence/docker/build-and-run.md) is captured terminal output of the container demo you cannot run |
| **They only have 2 minutes** | Cold `curl $BASE/healthz` (0:20) → `curl $BASE/metrics` and the sample-size point (0:30) → the four-row SharePoint table (0:40) → the alert that didn't fire (0:30) |
| **They want depth on one thing** | The alert defect and the OIDC subject are the two with the most in them. Both are written up in full — [`evidence/monitoring/alerting.md`](../evidence/monitoring/alerting.md) and [`DEPLOYMENT.md` § Things that will bite you](DEPLOYMENT.md#things-that-will-bite-you) |

## Questions to expect right after, and where the answers are

- *"Why Container Apps and not Functions or AKS?"* →
  [`INTERVIEW-NOTES.md`](INTERVIEW-NOTES.md)
- *"Why two tenants?"* → same, and it is a workaround for a permission that
  cannot be obtained, made explicit rather than hidden
- *"Why does the ID-qualified OIDC subject matter?"* → same
- *"What happens when Graph throttles you?"* → same, and `app/graph.py`
- *"What would you do differently at 10× scale?"* → same
- *"What does it cost?"* → $0.00 observed, and say the caveat in the same breath:
  the subscription is hours old. [`COST.md`](COST.md)

**Claim nothing that is not measured.** Everything in this script is; the things
that are not are in the README's gaps table, and being able to point at that list
is worth more than any single demo step.
