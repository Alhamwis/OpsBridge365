# Demo script — about six minutes

A run sheet for walking an interviewer through OpsBridge365. It is deployed and
running, so this is a live demo, not a slide deck. **The `/healthz`,
`/demo/metrics` and `/metrics` responses quoted below were captured against the
live service on 2026-08-29**; anything older carries its own date. The numbers
behind `/metrics` move — it reads a rolling window over a live list — so read the
shape, not the digits, and do a dry run on the day.

```
BASE=https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io
```

Two of the three endpoints are public, so an interviewer can run them on their own
machine while you talk:

| Endpoint | Who can run it | What it costs upstream |
| --- | --- | --- |
| `GET /healthz` | anyone | nothing |
| `GET /demo/metrics` | anyone | nothing — synthetic data |
| `GET /metrics` | **you, with a bearer token** | at most one Graph read per 45 s |

The token is the presenter's step. If you do not have it on the day, say so and
run `/demo/metrics` instead — it carries the same response shape, and nothing
else in this sheet depends on the token.

---

## Before you start

```bash
git pull
export BASE=https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io
export METRICS_API_TOKEN='<the value you generated at bootstrap>'

curl -s "$BASE/healthz"                  # confirm it is up
python -m pytest -q                      # confirm green: 106 passed, 12 deselected
```

The token is the one you passed to `infra/bootstrap.bicep` as `metricsApiToken`
(`openssl rand -base64 32`). It lives in Key Vault and the container reads it
through its managed identity — **you cannot read it back out of the vault**, and
that refusal is a demo beat in its own right at 4:00. Keep your own copy in a
password manager. Losing it means a rotation, not a recovery: re-run
`bootstrap.bicep` with a new `metricsApiToken` and restart the revision.

Then **leave the API alone for a few minutes.** `minReplicas: 0` means the
replica shuts down when idle, and the cold start *is* the demo. Waking it right
before you present throws away the best twenty seconds you have. Confirm it is
actually asleep rather than assuming it:

```bash
az containerapp revision list -g rg-opsbridge365 -n opsbridge-api -o table
az containerapp replica list  -g rg-opsbridge365 -n opsbridge-api \
  --revision <active-revision> -o table          # empty means zero replicas
```

Have open, in this order: a terminal, [`STATUS.md`](STATUS.md), and the
[deploy workflow's run list](https://github.com/Alhamwis/OpsBridge365/actions/workflows/deploy.yml).
Open the run list, not a bookmarked run id — the point is that the newest run on
`main` is green, and a pinned id goes stale the next time you push. Close
everything else.

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

Then **do not fill the silence with an apology.** It will hang for about twenty
seconds. That pause is the most honest thing in the demo, so narrate it while it
is happening:

> "Nothing is running. There was no replica a moment ago, so Azure has to
> allocate one and start the container before anything can answer. That's
> `minReplicas: 0`."

```json
{"status":"ok","version":"0.1.0"}
```

> "Twenty-point-two seconds, measured today; a second run gave twenty-one-point-three.
> That is the price of an idle month costing nothing rather than nearly nothing.
> I'd rather show you that than a number I took against an already-running
> container — an earlier version of these docs claimed 714 milliseconds cold,
> which cannot have been measured against a sleeping app, and it was wrong.
>
> It's a real trade and it is wrong for some workloads. A user-facing API would
> set `minReplicas: 1` and pay for a warm replica. This is a metrics endpoint a
> dashboard polls on a schedule, so twenty seconds on the first call after an
> idle period is the correct thing to buy $0 with."

Immediately again:

```bash
time curl -s "$BASE/healthz"
```

> "248 milliseconds warm. Everything above that is scale-from-zero, once."

If they ask whether the cold start breaks monitoring:

> "It would, if the monitor were naive. The scheduled health check retries six
> times, twenty seconds apart — long enough that a cold start is never reported
> as an outage, short enough that a real outage still goes red and stays red."

One more, because it takes four seconds:

```bash
curl -s -o /dev/null -w '%{http_code}\n' "${BASE/https/http}/healthz"    # 301
```

> "Plain HTTP redirects. `allowInsecure: false` on the ingress."

*(The 301 was captured 2026-08-18 and is a property of the ingress configuration
rather than of a deployment; run it in your dry run anyway.)*

## 1:30 — The three endpoints, and why they differ (75 seconds)

**This is the part that changed most recently, and it is worth the time.**

### The public one, with synthetic data

```bash
curl -s "$BASE/demo/metrics"
```

Abridged — the fields that matter, straight out of `app/demo.py`:

```json
{"open_tickets":7,"due_within_30min":2,"sla_compliance_7d_pct":90.9,
 "resolved_last_7d":12,"sla_measured_last_7d":11,
 "synthetic":true,"notice":"Synthetic sample data. Not from any Microsoft 365 tenant."}
```

> "That is the shape of the API with fabricated numbers. It makes no Graph call
> and touches no tenant, so anyone can hit it and it can't be turned into an
> amplifier. And `synthetic: true` is in the *body*, not a header — a screenshot
> keeps the body and drops the headers, and a screenshot is how a number ends up
> in somebody's deck."

### The closed one, refusing

```bash
# No token at all
curl -s -o /dev/null -w '%{http_code}\n' "$BASE/metrics"                 # 401

# A token, but the wrong one. Set it to any string you like.
WRONG=$(printf 'not-the-real-token')
curl -s -o /dev/null -w '%{http_code}\n' \
     -H "Authorization: Bearer ${WRONG}" "$BASE/metrics"                 # 401
```

> A note on why the wrong token is built from a variable rather than typed
> inline: writing a literal `Authorization: Bearer <something>` into a tracked
> file trips gitleaks' `curl-auth-header` rule, and the pipeline's secret scan
> is a hard gate. It fired on exactly this line during review. The right
> response was to change the line, not to widen the allowlist — an allowlist
> entry added for a documentation example is an allowlist entry that will
> one day cover a real credential.

> "No token: 401. Wrong token: also 401, same generic body. The response never
> tells a caller *which* of the two they got wrong. The audit line that says which
> one it was is written server-side, and it logs no part of the token.
>
> This endpoint used to be public. That was three problems at once: real ticket
> counts were world-readable, every anonymous request minted an Entra token and
> read a live SharePoint list — so one cheap request became several upstream
> calls — and a loop could hold a scale-to-zero container permanently awake and
> bill me for it. Authentication is what closes all three."

If they push on the token being a shared secret, do not defend it:

> "It's a static bearer token, and that is weaker than short-lived Entra tokens.
> I considered the platform's built-in Entra auth and didn't take it, because it
> validates above the process — none of the behaviour I actually wanted to prove,
> the 401, the 429, the cache accounting, could be tested offline. Rotation is a
> Key Vault update plus a revision restart. It's in the gaps list with the
> upgrade path."

### The live one, with the token

```bash
curl -s -H "Authorization: Bearer $METRICS_API_TOKEN" "$BASE/metrics"
```

```json
{"open_tickets":2,"due_within_30min":0,"sla_compliance_7d_pct":null,"resolved_last_7d":0,"sla_measured_last_7d":0}
```

*(Captured 2026-08-29. The shape is fixed; the numbers are a rolling 7-day window
over a live list, so do not memorise them.)*

> "Read live from SharePoint through Microsoft Graph. And look at the compliance
> field — it is `null`. Not zero, not a hundred. Nothing was resolved in the last
> seven days, so there is no compliance figure to report and the endpoint refuses
> to invent one. A dashboard showing a confident 100% because nothing happened is
> worse than a blank.
>
> The two counts next to it are why you can trust that: `resolved_last_7d` and
> `sla_measured_last_7d` are both zero, so the percentage ships with its own
> denominator and you can see there is nothing behind it. In August the same
> endpoint returned 50.0 with a denominator of two — one of two resolutions met
> its target. The numbers moved because the window is real."

Then the rule that goes with it, if they are interested:

> "A ticket resolved with no due date counts as resolved but stays out of the
> denominator, rather than being assumed to have met its target — that would
> inflate the number in exactly the direction that flatters me."

### The cache, in one command

Run it twice inside 45 seconds:

```bash
curl -s -D - -o /dev/null -H "Authorization: Bearer $METRICS_API_TOKEN" \
     "$BASE/metrics" | grep -i -E 'x-cache|cache-control'
```

```
x-cache: MISS
cache-control: private, max-age=45
```
```
x-cache: HIT
cache-control: private, max-age=31
```

*(`max-age` counts down whatever is left of the 45-second TTL, so the second
number depends on how long you took.)*

> "45-second cache, and concurrent misses collapse into one upstream fetch rather
> than a stampede. So a dashboard polling every five seconds costs Graph one read
> every 45 seconds instead of twelve a minute. `private` because it's tenant
> data — no shared proxy gets to store it."

**Optional, if they ask about abuse:** 30 requests a minute per caller, sliding
window. A loop shows it, and you have already spent a few of the 30, so the 429
arrives before the thirtieth:

```bash
for i in $(seq 1 31); do
  curl -s -o /dev/null -w "$i:%{http_code} " \
       -H "Authorization: Bearer $METRICS_API_TOKEN" "$BASE/metrics"
done; echo
```

> "429 with a `Retry-After`, and the whole loop cost one Graph read because of the
> cache. Worth naming the limit though: the counter is in-process, so it is per
> replica. That is exactly right at `maxReplicas: 1` and becomes wrong the moment
> it scales out."

## 2:45 — The end-to-end proof (75 seconds)

**This is the headline. Do not rush it.**

> "The sync job runs on a cron in Azure — `0 */6 * * *`, a Container Apps *Job*,
> so it starts, runs, exits, and bills nothing in between. Here's what happened
> the first time it ran against the real tenant."

Show the table — captured **2026-08-18**, in
[`evidence/sharepoint/reconciliation.md`](../evidence/sharepoint/reconciliation.md)
— or the live SharePoint list if you have it open. The device names and the
assigned user are synthetic sample data:

| Asset | AssignedUser | ComplianceStatus |
| --- | --- | --- |
| `CONTOSO-LT-001` | Dana Whitfield | Compliant |
| `CONTOSO-LT-002` | Unknown | Unknown |
| `CONTOSO-DT-003` | Unknown | Unknown |
| `CONTOSO-TB-004` | Unknown | Unknown |

> "The tenant genuinely had zero devices, so the first cloud run had nothing to
> match and everything stayed `Unknown` — it did not invent data to look busy. So
> I created **one** device in Entra with a registered owner and ran the job
> again. One row got a real user and a real compliance status. Three stayed
> `Unknown`. Both behaviours proven by one run."

The job's own summary, captured by Log Analytics on the same day:

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

## 4:00 — Security, in two commands' worth of talking (45 seconds)

Open the [deploy workflow run list](https://github.com/Alhamwis/OpsBridge365/actions/workflows/deploy.yml)
and click the newest run on `main`. Do not open a bookmarked run id.

> "Push to main runs the tests, `ruff`, and a gitleaks scan over the *full
> history*. All three are hard gates — lint used to be advisory and isn't any
> more. Only then does it build and push the image, and only then does it deploy.
> On top of that: CodeQL, a Trivy scan of the filesystem and of the built image,
> an SBOM as a build artifact, every Action pinned to a commit SHA rather than a
> tag, and Python dependencies installed from a hash-pinned lock.
>
> The deploy authenticates to Azure with OIDC federation — GitHub mints a
> short-lived token, Azure exchanges it, the token dies with the job. There is no
> Azure password anywhere in this repository or in its secrets."

Then the two that land hardest:

> "There are two app registrations in two tenants. The one that can deploy has no
> credential at all — zero passwords, zero certificates. The one with a secret has
> no Azure permissions whatsoever. So the identity with authority has nothing to
> leak, and the credential that can leak has no authority.
>
> And the pipeline now only needs Contributor. Creating the Key Vault role
> assignment needs a much stronger role, so that step moved into a separate
> template a human runs once — `bootstrap.bicep`. The routine deployment never
> creates a role assignment and never sees a secret value; it only references the
> vault. The standing elevated grant comes off the service principal once the
> new templates have deployed green — I'd rather say 'scheduled' than claim a
> privilege is gone before I've taken it away."

Now do it live, because a refusal is better than an assertion:

```bash
VAULT=$(az keyvault list -g rg-opsbridge365 --query "[0].name" -o tsv)
az keyvault secret show --vault-name "$VAULT" --name metrics-api-token
```

```
(Forbidden) ... does not have secrets get permission on key vault ...
Code: ForbiddenByRbac
```

*(Abridged. The code is the part that matters, and it was re-verified 2026-08-29.)*

> "That secret lives in Key Vault, and the container gets a Key Vault *reference*,
> not a value — the app definition has a vault URL and no plaintext. And when
> **I** try to read it as myself, Key Vault says `ForbiddenByRbac`. Only the
> managed identity can. That's the demonstration — least privilege that refuses
> *me*, including the token you just watched me use."

## 4:45 — The parts that didn't work (60 seconds)

**Volunteer this. It is the most credible minute in the demo.**

> "The deploy didn't work first time. Four attempts failed for four genuinely
> different reasons, and none of them was a code problem — `bicep build` was clean
> through all of it.
>
> First: `AADSTS700213` on the OIDC login. The deploy job declares a GitHub
> environment, and that changes the subject GitHub presents to
> `environment:production`, not `ref:refs/heads/main`. My credential matched the
> branch. Second: same error again, because this account's subject is
> *ID-qualified* — the owner and repo carry numeric id suffixes, and
> `use_default` was already true, so there was nothing to normalise. Entra has to
> match that exact string. That form is rename-proof, so it's actually the better
> configuration. Third: `RequestDisallowedByAzure` — Azure for Students has an
> allowed-regions policy and `eastus` isn't on it. Fourth:
> `MissingSubscriptionRegistration` — five resource providers were unregistered
> on the fresh subscription, and ARM names one per failure, so it arrives as a
> sequence."

All four are written up with their fixes in
[`DEPLOYMENT.md` § Things that will bite you](DEPLOYMENT.md#things-that-will-bite-you),
and the run that finally went green — `32115509179`, on 2026-08-18 — is in
[`evidence/github-actions/pipeline.md`](../evidence/github-actions/pipeline.md).

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

## 5:45 — Where it stands (30 seconds)

Open [`STATUS.md`](STATUS.md): a table of what was verified, when, and the exact
command — then the *Not measured* list immediately below it.

> "Everything in the top table has a date and a command, so you can re-run it and
> expect drift rather than taking my word for it. Anything older sits in a
> separate historical table, because a service being healthy in August isn't
> evidence about today.
>
> And the gaps are stated right underneath. The $0.00 spend was observed on
> 2026-08-18 against a $20 budget and has not been re-measured, so it is days of
> evidence and not a billing cycle. The end-to-end run was one user and one
> device. There is no uptime percentage, because six probes a day cannot support
> one. `/metrics` uses a static bearer token, which is weaker than Entra. And the
> Microsoft 365 tenant is a **trial** with a lifecycle date of 2026-09-16 — after
> that, the live-tenant half of this stops being reproducible unless I convert it.
> I'd rather tell you all of that than have you find it."

---

## Fallbacks

| If this fails | Do this |
| --- | --- |
| **The live API doesn't respond** | Give it the twenty seconds it needs and say why: *"scale-from-zero, this is the cold start."* If it still fails, switch — the captured responses are in [`evidence/azure/deployment.md`](../evidence/azure/deployment.md) (labelled historical), and the local container demo below runs with no network at all |
| **You don't have the token** | Say so and skip the live `/metrics` beat. `/demo/metrics` carries the same response shape, and the 401 beat still works — it is arguably the better demonstration anyway |
| **No network** | Build the image **before** you travel — `docker build` pulls a base layer and needs a network; the run does not. Then `docker run --rm -p 8000:8000 opsbridge365:local` and, with no configuration at all: `curl localhost:8000/healthz` → 200 with **zero credentials**, `curl localhost:8000/demo/metrics` → 200 synthetic, `curl localhost:8000/metrics` → **503**, naming no variable to the caller. That 503 is the point — an unset token does not reopen the endpoint. `docker run --rm opsbridge365:local id -u` → `10001` |
| **Docker is down too** | `python -m pytest -q` → 106 passed, then walk the code: `metrics.py` for the SLA logic, `sync.py` for the ambiguity rule, `security.py` for the auth-then-rate-limit ordering, `cache.py` for single-flight. [`evidence/docker/build-and-run.md`](../evidence/docker/build-and-run.md) is captured terminal output of the container demo you cannot run |
| **They only have 2 minutes** | Cold `curl $BASE/healthz` (0:25) → `curl $BASE/demo/metrics` (0:20) → unauthenticated `/metrics` → 401 (0:15) → authenticated `/metrics` and the `null` point (0:30) → the four-row SharePoint table (0:30) |
| **They want depth on one thing** | The alert defect, the OIDC subject, and closing `/metrics` are the three with the most in them. The first two are written up in [`evidence/monitoring/alerting.md`](../evidence/monitoring/alerting.md) and [`DEPLOYMENT.md` § Things that will bite you](DEPLOYMENT.md#things-that-will-bite-you); the third is in [`SECURITY.md`](SECURITY.md) and [`INTERVIEW-NOTES.md`](INTERVIEW-NOTES.md) |

## Questions to expect right after, and where the answers are

- *"Why Container Apps and not Functions or AKS?"* →
  [`INTERVIEW-NOTES.md`](INTERVIEW-NOTES.md)
- *"Why is the cold start twenty seconds?"* → same, and say the trade out loud
  rather than defending the number
- *"Why two tenants?"* → same, and it is a workaround for a permission that
  cannot be obtained, made explicit rather than hidden
- *"Why does the ID-qualified OIDC subject matter?"* → same
- *"What happens when Graph throttles you?"* → same, and `app/graph.py`
- *"Why a shared bearer token instead of Entra?"* → same, and
  [`SECURITY.md`](SECURITY.md)
- *"Why two Bicep templates?"* → same, and the short version is that one role
  assignment was forcing the pipeline to hold a permission it needed exactly once
- *"What would you do differently at 10× scale?"* → same
- *"Did you use AI to build this?"* → answer it in one sentence and move on:
  *"I designed, deployed and verified the system, using Claude Code as an
  engineering assistant. I can explain and reproduce every major architectural
  and security decision."* The long version, including what that meant in
  practice, is in [`INTERVIEW-NOTES.md`](INTERVIEW-NOTES.md)
- *"What does it cost?"* → $0.00 observed on 2026-08-18 against a $20 budget, and
  say the caveat in the same breath: that is days of evidence, not a billing
  cycle. [`COST.md`](COST.md)

**Claim nothing that is not measured.** Everything in this script is, and every
number in it carries the date it was taken. The things that are *not* measured
are in [`STATUS.md`](STATUS.md); being able to point at that list is worth more
than any single demo step.
