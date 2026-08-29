# Interview notes

Questions this project invites, and honest answers. Where an answer is a design
intention rather than something observed running, it says so. Most of it is now
observed: the system is deployed in Azure, the pipeline deploys it, and the
numbers quoted below were measured rather than estimated.

**Every measurement here carries the date it was taken.** Live-service figures
were re-measured on **2026-08-29**; anything older is labelled with its capture
date and is history, not a status claim. [`STATUS.md`](STATUS.md) holds the
current-state table and the command behind each row — if a number here and a
number there disagree, that file is the one with the command attached.

---

## "Why Azure Container Apps? Why not Functions, or AKS?"

**Functions** was the real alternative and would work. A timer-triggered Function
for the sync and an HTTP-triggered one for `/metrics` is close to the same shape,
and the Consumption plan also scales to zero. I chose Container Apps for three
reasons:

1. **The artifact is a container.** The same image runs on my laptop, in CI, and in
   Azure, byte for byte. With Functions I would be deploying a Python app into a
   host runtime I do not control and cannot reproduce locally with the same
   fidelity. When the local container works, I know what I am shipping.
2. **The API and the job share code.** `graph.py`, `sharepoint.py`, `models.py` and
   `config.py` are used by both. One image with two entrypoints keeps them from
   drifting; two Function apps would mean either a shared package to version or
   duplicated clients.
3. **Portability.** Nothing in the application knows it is on Azure. It is a
   FastAPI app and a `python -m` module. Moving to Cloud Run, ECS or plain Docker
   is a deployment change, not a rewrite. The Functions programming model is a
   one-way door.

**AKS** was never a serious candidate. A control plane, node pools, and a cluster
to patch, for two workloads that together run for a few minutes a day. The
cheapest realistic AKS setup costs more per month than this entire architecture
costs per year, and it is operational burden bought with nothing to spend it on.
AKS earns its keep when you have many services, real traffic, and a platform team.

**What Container Apps costs me:** less control than AKS, and cold starts. Both are
correct trades here, and I would revisit the Functions comparison if the workload
became many small event handlers rather than two containers. The cold start is
not a footnote — it is twenty seconds, and it has its own answer below.

---

## "Twenty seconds of cold start? Why isn't that sub-second?"

Because the container is genuinely not running, and starting one takes that long.

Measured **2026-08-29**, with the replica count observed at **0** immediately
before the probe so it is a real scale-from-zero: **20.2 s** cold, **248 ms**
warm. A second run gave 21.3 s. That is `minReplicas: 0` — Azure allocates a
replica, starts the container and starts uvicorn before the first byte comes
back.

**An earlier revision of this repository claimed 714 ms cold, and that was
wrong.** It cannot have been measured against a sleeping app; it was almost
certainly a warm or partially-warm request labelled as cold. I would rather
correct it in public than keep a flattering number, because it is the single
easiest claim in the repo for an interviewer to check — one `curl` and it fails.

**The trade, stated plainly.** Idle cost is the reason. A replica that never
sleeps is a replica that always bills; at `minReplicas: 0` an idle month costs
nothing and the first caller after an idle period pays twenty seconds. For a
metrics endpoint that a dashboard polls on a schedule, that is the right purchase.
For a user-facing API it is not, and the fix is one line — `minReplicas: 1` — plus
the bill that comes with it. The 45-second cache on `/metrics` matters here too:
a poller keeps the container awake and warm during the day, and lets it sleep at
night.

**What it forces elsewhere.** Nothing that touches this service may treat a slow
first response as a failure. The scheduled health check retries six times, twenty
seconds apart, before it goes red, and the deploy's post-deploy check retries for
about three minutes. Those windows are a necessity, not a courtesy — they have to
be sized against the real cold start, not against 714 ms.

---

## "How is the SLA metric actually computed?"

`app/metrics.py`, and it is a pure function over ticket rows — no I/O, which is why
it is the most thoroughly tested part of the codebase.

Given a list of tickets and a "now":

- **`open_tickets`** — every ticket whose `Status` is not `Resolved`, compared
  case-insensitively with whitespace trimmed.
- **`due_within_30min`** — unresolved tickets whose `SLAResolutionDue` falls
  between now and now + 30 minutes. Already-overdue tickets are deliberately *not*
  counted here; this is "about to breach", not "has breached".
- **`sla_compliance_7d_pct`** — of tickets resolved in the last 7 days, the share
  where `ResolvedDate <= SLAResolutionDue`, rounded to one decimal.

Three decisions inside that last one are the interesting part:

1. **A zero denominator returns `null`, not 0% or 100%.** If nothing was resolved
   in the window there is no compliance figure, and inventing one is worse than
   admitting it. A dashboard showing a confident 100% because nothing happened is
   actively misleading.
2. **A ticket resolved with no due date is excluded from the denominator.** It is
   counted in `resolved_last_7d` but not in `sla_measured_last_7d` — there is no
   target to measure it against, and defaulting it to "met" would inflate the
   number in exactly the direction that flatters you.
3. **Both counts are returned alongside the percentage.** `resolved_last_7d` and
   `sla_measured_last_7d` let a consumer see that "100%" came from two tickets.
   A percentage without its sample size is a half-truth.

Timestamps: SharePoint stores UTC, so a naive timestamp is treated as UTC rather
than as local time. `now` is injectable, which is how the window boundaries are
tested deterministically.

**The honest limitation:** this reads whatever the Tickets list says. If a ticket
is resolved in real life and nobody sets `ResolvedDate`, the metric does not know.
Garbage in, honest garbage out — the code will not paper over it.

**The zero-denominator case is not hypothetical.** On 2026-08-18 the live
endpoint returned `sla_compliance_7d_pct: 50.0` with `sla_measured_last_7d: 2` —
one of two measurable resolutions met its target. On 2026-08-29 the same endpoint
returns `null`, with `resolved_last_7d: 0` and `sla_measured_last_7d: 0`, because
the rolling seven-day window has moved past those resolutions. Nothing broke.
That is the design working, and it is a better demonstration than the 50% was.

---

## "Why did you put authentication in front of `/metrics`? It used to be public."

Because "public" was three separate problems wearing one coat, and only one of
them was about privacy.

1. **Disclosure.** It returned real ticket counts from a real tenant to anybody
   who knew the URL. Counts are not ticket contents, but "how far behind is this
   service desk right now" is still operational information about somebody's
   business.
2. **Amplification.** Every anonymous request built a new MSAL application,
   acquired a token and read a live SharePoint list. One cheap HTTP request that
   costs the caller nothing turned into several upstream calls that cost me
   Microsoft Graph throttling budget. That is the shape of an amplifier.
3. **Cost and availability.** The whole cost argument for this architecture is
   that the container sleeps. A loop against a public endpoint holds a
   scale-to-zero container permanently awake, on `maxReplicas: 1`, at 0.25 vCPU.
   It is a denial-of-wallet and a denial-of-service in the same request.

**What it is now.** A bearer token compared in constant time, then a sliding
30-requests-per-minute limit per caller, then a 45-second cache, all inside a
25-second wall-clock deadline. The order is deliberate: authenticate *first*, so
an unauthenticated flood cannot spend the rate-limit budget of a legitimate
caller sharing an egress address.

**It fails closed.** If `METRICS_API_TOKEN` is not configured the endpoint returns
**503** and serves nothing. A misconfiguration must never silently reopen the hole
the authentication closes, and there is a test that deletes the variable and
asserts the 503.

**Errors are generic on purpose.** Missing token and wrong token both return the
same 401 with the same body. The response never tells a caller which of the two
they got wrong. The audit line that records which one is written server-side and
contains no part of the token.

**The trade-off I would want to be asked about.** A static bearer token is weaker
than short-lived Entra tokens, and I know it. Container Apps' built-in Entra
authentication (EasyAuth) was the alternative and I did not take it, for two
reasons. It validates tokens in the platform, *above* the process — so none of the
behaviour I actually wanted to prove (401 on a bad token, 429 under load, cache
accounting) could be covered by the offline test suite; the control would exist
but nothing in the repository could demonstrate it. And it needs a second app
registration in a college-managed tenant, which is the same closed door that
produced the two-tenant split in the first place. Rotation today is: update the
Key Vault secret, restart the revision. The upgrade path is in
[`SECURITY.md`](SECURITY.md), and the weakness is in the gaps list rather than
buried.

**The public surface did not disappear.** `GET /demo/metrics` returns the same
response shape with synthetic values, marked `"synthetic": true` in the *body*
rather than in a header, because a screenshot keeps the body and drops the
headers. It makes no upstream call, so it cannot be used to amplify anything, and
a recruiter following the README still sees a working API without me handing out
a credential.

---

## "Why a 45-second cache and single-flight coalescing? Is that not premature?"

It would be premature as a performance optimisation. It is not an optimisation —
it is the bound on how much upstream work an authorised caller can cause.

**The TTL.** Without it, every request is at least one SharePoint list read, so a
dashboard polling every five seconds costs Graph twelve reads a minute forever.
With 45 seconds that same dashboard costs at most one read every 45 seconds, and
a ticket resolved right now still shows up in under a minute. SLA counts do not
need to be accurate to the second; pretending they do would be buying precision
nobody asked for with somebody else's throttling budget.

The same change fixed a second waste that was easy to miss. The Graph client used
to be built per request, which meant a fresh MSAL confidential-client application
— and therefore tenant discovery and a token mint, against an empty token cache —
on **every single call**. It is now one client for the process lifetime, so the
token is reused until it expires. The cache bounds the SharePoint reads; the
shared client bounds the token traffic.

**Single-flight is a separate problem from the TTL, and this is the part worth
asking about.** A plain TTL cache still lets a burst of callers that all miss at
the same instant issue N upstream calls — the classic cache stampede, and the
moment it happens is exactly the moment you least want it: the cache has just
expired and traffic has just arrived. Here the first caller to miss starts the
fetch and every concurrent caller awaits *that same task*. N callers, one Graph
read.

Three details that took thought:

- **A failed fetch is not cached.** The exception propagates to every caller
  waiting on that flight and the next call retries. Caching a failure for 45
  seconds would turn one bad moment into 45 seconds of guaranteed failure.
- **One caller going away must not cancel the flight.** The shared task is
  awaited under `asyncio.shield`, so a client that disconnects mid-request does
  not take the other waiters' fetch with it.
- **The cache is unkeyed.** This service computes exactly one derived value, so a
  keyed cache would add a dictionary and an eviction policy to hold a single
  entry. When there is a second value, that is when it earns a key.

The deadline sits above all of it: 25 seconds of wall clock for the whole
refresh. `app/graph.py` bounds each individual request (30 s, 3 attempts) but not
the aggregate — a deeply paged list could otherwise run for minutes while the
caller's connection is held open, which is the same denial-of-wallet shape the
authentication closed.

**What I would say against it.** The rate limiter's counters are in-process, so
the limit is per replica. That is exactly correct at `maxReplicas: 1` and becomes
wrong the moment the service scales out — at which point the limiter needs shared
state (Redis, or the ingress). The docstring says so, and it is in the gaps list
rather than discovered later by somebody else.

---

## "How do you handle retries and throttling?"

One function, `request_with_retry` in `app/graph.py`, and the SharePoint client
uses it rather than growing its own copy. Three attempts by default.

**What is retried:**

- **429 and 503**, honouring the `Retry-After` header when the server sends a
  usable one, capped at 60 seconds so a hostile or mistaken header cannot park the
  job for an hour.
- **Timeouts** (`httpx.TimeoutException`) and **transport errors**
  (`httpx.TransportError`) — connection resets, DNS failures.

**What is not retried:** every other 4xx and 5xx raises `GraphHTTPError`
immediately. A 403 is missing consent and a 404 is a wrong list id; retrying either
is just a slower way to fail. Token acquisition failures raise `GraphAuthError`
with no retry — bad credentials do not become good ones.

**Backoff** is exponential: 1s, 2s, 4s. When `Retry-After` is present it wins.

**Paging** follows `@odata.nextLink` with a hard ceiling of 1,000 pages, because a
paging loop that cannot terminate is a worse failure than one that gives up. HTTP
timeout is 30 seconds per request.

**Above that**, the Container Apps Job sets `replicaTimeout: 1800` and
`replicaRetryLimit: 1` — so a hung Graph call cannot bill for more than 30 minutes,
and a failed sync gets one retry and then waits for the next cron tick instead of
hammering an outage. That is safe because the sync is idempotent: it PATCHes by
list item id, so re-running it converges rather than duplicating.

**What I would say honestly about it:** the retry logic is verified against
simulated responses — `respx` intercepts httpx, so `tests/test_graph.py` covers
429 with and without `Retry-After`, 503, timeouts, transport errors, non-retryable
4xx and malformed JSON. The client itself has met the real Graph API — on
2026-08-18, twelve integration tests and a cloud sync job against a live tenant —
but at a scale where **nothing throttled**. So the happy path is proven against
Microsoft, at that date and that size, and the
retry path is still proven only against mocks. Real throttling behaviour at
volume, and how often Graph actually sends `Retry-After` versus expecting you to
back off blindly, is something I would expect to learn in the first week of
running it against a real fleet.

One trade I made knowingly: transport errors are retried on `PATCH` as well as on
reads. That is safe *here* because the PATCH is idempotent by construction. In a
system with non-idempotent writes I would need request-level idempotency keys.

---

## "What would you do differently at 10× scale?"

At 10× (say a few thousand devices) the first thing that breaks is the full fetch.

1. **Delta queries.** `list_users` and `list_devices` pull *everything* on every
   run. Graph's `/delta` endpoints return only what changed since a stored token.
   That is the single highest-value change: it turns runtime from a function of
   fleet size into a function of churn, and it takes the throttling pressure with
   it.
2. **Server-side filtering and narrower `$select`.** The device call currently
   selects everything and expands `registeredOwners`. Asking for fewer fields is
   free and immediate.
3. **Batching.** Graph's `$batch` endpoint takes up to 20 requests per call. The
   sync currently issues one PATCH per matched device, sequentially. At thousands
   of devices that is the dominant cost — batching plus bounded concurrency (not
   unbounded, which just converts into 429s) is the fix.
4. **Stop holding everything in memory.** `list_users` and `list_devices` return
   full lists and `index_users` builds a dictionary over all of them. Fine at 0.5
   GiB and hundreds of records; not fine at hundreds of thousands. Streaming pages
   and processing incrementally is the rewrite.
5. **Rethink the ambiguity rule.** "An ambiguous key matches nothing" is right at
   small scale and silently drops data at large scale. At 10× I would want the
   unmatched and ambiguous sets *reported* — pushed to a dead-letter list or an
   alert — rather than only counted in a JSON summary nobody reads.
6. **Persistent state.** The job is stateless by design, which is why it is
   simple. Delta tokens need somewhere to live — Azure Table Storage or a blob is
   the cheap answer.
7. **Raise `maxReplicas`, and move the rate limiter off in-process state.** The
   caching half of this item is now done — `/metrics` is served from a 45-second
   single-flight cache, so a burst of callers costs one Graph read rather than
   one each. What is *not* done is scaling out: the rate limiter counts in
   process, so at `maxReplicas: 1` the 30-per-minute bound is exact, and the
   moment a second replica exists it becomes 30 per minute per replica. Shared
   state (Redis, or a limit enforced at the ingress) is the fix, and it has to
   land before `maxReplicas` is raised, not after.

And operationally: alerting on job failure (deployed, and it took a deliberate
failure to discover the first version of the query would not have fired), a dead
letter for failed PATCHes, and dashboards on the unmatched count — because a sync
that silently matches less and less is the failure mode that hides longest.

---

## "What is deliberately out of scope?"

Being able to name this is half the point of scoping.

- **The Power Platform half.** The spec describes a service desk — Power Apps,
  SharePoint lists, Power Automate flows, Teams notifications — in a separate
  tenant. None of it is in this repository, and this repository never claims it is.
- **Federated identity on `/metrics`.** The endpoint *is* authenticated now — a
  bearer token, a rate limit and a cache — but the token is a static shared
  secret held in Key Vault, not a short-lived Entra token. Per-caller identity,
  scopes and expiry are out of scope for this deployment. The reasoning, and what
  it would take to change, is under the `/metrics` question above.
- **A UI.** `/metrics` returns JSON. A dashboard is a presentation problem, and
  building a mediocre one would have added surface without adding evidence of
  anything.
- **Writing back to Graph.** The sync is one-directional: Graph is the source of
  truth, SharePoint is the projection. Two-way sync means conflict resolution, and
  conflict resolution means a product decision nobody has made.
- **Multi-tenant support.** Site id and list ids are configuration, so the same
  image runs against any tenant — but one deployment serves one tenant. Real
  multi-tenancy means per-tenant credentials and isolation, which is a different
  system.
- **Ticket creation or mutation.** Read-only on tickets, on purpose. The service
  desk owns that data.
- **Intune device data.** `isCompliant` on a directory device object is coarse.
  `deviceManagement/managedDevices` is much richer and needs a licence I do not
  have.
- **Runtime and dependency scanning beyond the build.** CodeQL, Trivy
  (filesystem and image), an SPDX SBOM, Dependabot and dependency review on PRs
  all run in CI now, and gitleaks scans the full git history and hard-gates the
  image build. What is still out of scope is anything that watches the *running*
  system: no runtime vulnerability agent, no image re-scan after publication, no
  admission policy. A vulnerability disclosed the day after a build is not
  something this pipeline notices until the next push or the next weekly
  Dependabot run.

---

## "Did you actually deploy it, or is this a repo full of YAML?"

Deployed, running, and measured. Push to `main` runs tests, `ruff`, gitleaks over
the full history, CodeQL and Trivy, then builds and pushes the image, then deploys
through OIDC federation with no stored Azure credential. The evidence is the
[run list on `main`](https://github.com/Alhamwis/OpsBridge365/actions/workflows/deploy.yml)
rather than a run id I pasted here — a pinned id is stale the next time I push,
which is exactly how this document used to cite a run from 2026-08-18 as though it
were current.

Checked on 2026-08-29: the API answers at
`https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io`, there
are eight resources in `rg-opsbridge365`, and the idle replica count is zero. The
sync job has run on its cron in Azure and written a live SharePoint list — that
was captured on 2026-08-18 and has not been re-run since. Observed spend was
$0.00 against a $20 budget, also on 2026-08-18.

**It did not work first time, and that is the part worth asking about.** Four
deploy attempts failed for four genuinely different reasons — an OIDC subject that
turned out to be environment-scoped, then a subject that turned out to be
ID-qualified, then a subscription policy that forbids `eastus`, then five
unregistered resource providers on a fresh subscription. `bicep build` returned
zero diagnostics through every one of them. The template was never the problem.
The first fully green run, `32115509179` on 2026-08-18, is the one that story
ends at.

The general point I would make from it: **a template that compiles tells you
nothing about whether your subscription will accept it.** Every one of those
failures lived in the gap between a repository and one specific real
subscription — an identity provider's default claim format, a policy assignment,
a subscription's initial provider state. None of them is discoverable by reading
code, and all four are written up in
[`DEPLOYMENT.md` § Things that will bite you](DEPLOYMENT.md#things-that-will-bite-you).

**What I still would not claim.** The cost figure was observed once, on
2026-08-18, and has not been re-measured — it covers days, not a billing cycle,
and the $20 budget is the guardrail rather than the proof. The end-to-end run was
one user and one device. There is no load, throughput or uptime measurement, and
the scheduled health check deliberately publishes no uptime percentage because six
probes a day cannot support one. Those are in [`STATUS.md`](STATUS.md) under *Not
measured*, and I would rather point at them than have someone find them.

---

## "Why two Entra tenants? That looks like over-engineering."

It is the opposite — it is a workaround for a permission I cannot get, made
explicit. Microsoft Graph *application* permissions need tenant admin consent. My
institution will not grant a student's app registration `User.Read.All` and
`Device.Read.All`, and it should not. The only way to demonstrate app-only Graph
access is to be the admin of the tenant you consent in, so I create one.

The consequence is stated in [`ARCHITECTURE.md`](ARCHITECTURE.md) rather than
hidden: the sync writes
to a SharePoint site in *my* tenant, not the institutional one, because
cross-tenant writes need consent from the same closed door. The site id and list
ids are configuration, so the same image deploys against any tenant that grants
consent — which is how a vendor ships this anyway.

**And then it turned into a security property.** Once the tenants were separate,
the two identities had to be separate too, and they landed in different
directories: `opsbridge-deploy` lives in the college-managed tenant that owns the
Azure subscription, and `opsbridge-graph` lives in the Microsoft 365 tenant that
owns the data. That split is what makes the sentence "the identity with Azure
authority has no credential, and the credential that can leak has no Azure
authority" literally true rather than aspirational — they are not merely
different app registrations with different role assignments, they are principals
in different directories that cannot be conflated by a careless role assignment.

The cost is that two tenant ids exist and **must never be swapped**.
`AZURE_TENANT_ID` appears in exactly one step of `deploy.yml`;
`GRAPH_TENANT_ID` becomes the Bicep parameter, then the container's environment,
then the MSAL authority. Reuse one for the other and ARM still deploys
successfully — the failure surfaces later, as every Graph call rejecting a token
from a directory the app does not exist in. That is a nasty failure mode, so both
the workflow and `DEPLOYMENT.md` say so at the point of use rather than in a
footnote.

So: a workaround for a permission I cannot get, which turned into a blast-radius
boundary I would want anyway. I would keep the split even in a tenant where I
was admin.

One dated caveat that belongs with this answer: the Microsoft 365 tenant runs on
an **`O365_BUSINESS_PREMIUM` trial**, `isTrial: true`, with a lifecycle date of
**2026-09-16**. Earlier revisions of these docs claimed it was a paid plan and
concluded there was no deadline on capturing tenant evidence. That was wrong on
both counts. If it lapses, `/healthz` and `/demo/metrics` keep working,
`/metrics` returns 502 rather than serving stale numbers as live, the sync job
fails and the alert rule catches it, and the Azure side is untouched — different
tenant, still $0.00. [`STATUS.md`](STATUS.md) carries the full table.

---

## "Why is the Bicep split into `bootstrap.bicep` and `main.bicep`?"

Because one role assignment was forcing the continuous-deployment identity to
hold a permission it needed exactly once.

`main.bicep` used to create the Key Vault "Secrets User" role assignment for the
container identity. `Microsoft.Authorization/roleAssignments` cannot be created by
Contributor, so the GitHub deployment identity had to hold a role-administration
permission — **permanently, on every push**, to create an assignment that only
ever needed creating once. It also took the Graph client secret as a parameter,
so GitHub had to store that secret and hand it over on every deployment.

The split makes the privileged half explicit and rare:

- **`infra/bootstrap.bicep`** — run **once, by a human, from a workstation**. It
  owns the Key Vault, the user-assigned identity, the role assignment, and the two
  secret *values* (`graph-client-secret`, `metrics-api-token`).
- **`infra/main.bicep`** — routine. It references the vault and the identity as
  `existing`, creates Log Analytics, the Container Apps environment, the Job and
  the App, and contains **no role assignment and no `clientSecret` parameter**.

Three consequences, and they are the answer to "so what":

1. **The routine deployment identity needs only Contributor.** Role Based Access
   Control Administrator is no longer required on every push.
2. **`GRAPH_CLIENT_SECRET` is no longer a GitHub secret.** The value is supplied
   once, by hand, and after that the pipeline only references a vault URL. A
   secret that never enters CI cannot leak from CI.
3. **`bootstrap.bicep` is a hard prerequisite.** `main.bicep` fails on the
   `existing` lookup without it, which is the correct failure: a routine
   deployment that silently *creates* a vault is a routine deployment that can
   silently create the wrong one.

Both secret parameters are optional and independent, guarded by `if (!empty(...))`,
so rotating one does not touch the other and re-running the template with neither
changes nothing:

```bash
az deployment group create -g rg-opsbridge365 \
  --template-file infra/bootstrap.bicep \
  --parameters graphClientSecret=<value> metricsApiToken=<value>
```

Prefer a parameters file to the inline form above: an argument expanded on a
command line is a literal element of `az`'s argv, readable from `/proc` and by
any `ps` running at the same moment on a shared machine.

**The cost of the split.** There is now a one-time manual step, and a
`README`-shaped prerequisite that a reproducer can forget. I would rather have a
documented manual step that runs once than a permanent standing privilege on an
automated identity — the second one is invisible, and invisible privilege is the
kind nobody removes.

---

## "Why hash-pinned dependencies and SHA-pinned actions? Isn't a tag enough?"

A tag is a movable reference. That is the entire answer, and both halves of it
follow from the same sentence.

**Actions.** `uses: some/action@v3` resolves whatever `v3` points at *today*. The
deploy job holds `id-token: write` and the Azure subscription's trust; an upstream
tag that gets moved — by a maintainer, or by somebody who compromised a maintainer
— runs arbitrary code inside that job. Every action is now pinned to a full 40-char
commit SHA with the version in a trailing comment, so an upgrade is a commit in
this repository that a human reviews, and Dependabot proposes those upgrades
weekly instead of me pretending to track them.

**Python packages.** `fastapi>=0.110` is not a dependency specification, it is a
range, and it resolves differently on my laptop, in CI and in the Docker build —
on different days. `requirements.txt` and `requirements-dev.txt` are now fully
resolved, hash-pinned locks compiled from `pyproject.toml` for linux/py3.12: 31
runtime packages, 39 with dev. Installs use `--require-hashes --no-deps`, so a
package whose contents changed under a version number fails the install rather
than shipping. Regeneration is one command per file, recorded in
[`DEPLOYMENT.md`](DEPLOYMENT.md), because a lockfile nobody can regenerate is a
lockfile that will be deleted the first time it is inconvenient.

**The base image.** Same argument, so `python:3.12-slim` is pinned by digest with
the tag kept as a comment. Otherwise the same Dockerfile at the same commit
produces a different base layer every time Debian ships a security update, and
"reproducible build" means nothing.

**What this costs.** Pinning stops upgrades happening by accident, which means it
also stops *security* upgrades happening by accident. Pinning without a bump
process is just a stale tree with better paperwork, so the process is the other
half: Dependabot for pip, docker and github-actions, weekly and grouped; Trivy
scans the filesystem and the built image, and fails the build on CRITICAL or HIGH
findings **that have a fix available** — unfixable ones are reported and do not
block, because a gate nobody can pass gets switched off. And `ruff` is now a hard
gate rather than `continue-on-error: true`, so lint failures block the image build
and the deploy instead of turning the CI badge into decoration.

---

## "Why GHCR instead of Azure Container Registry?"

Cost, and then a second-order security win. ACR has no free tier — Basic is a fixed
monthly charge whether or not anything is pulled, and it would be the only
recurring cost in an architecture otherwise designed to sit at $0.

The second-order effect matters more: a **public** GHCR package needs no pull
credentials at all. So `main.bicep` has no `registries:` block, no registry
password in Key Vault, and no image-pull secret to rotate. Removing a cost also
removed a credential. And the push uses the built-in `GITHUB_TOKEN` — no PAT to
create or leak.

The trade is that the image is public, and it is: `ghcr.io/alhamwis/opsbridge365`
pulls anonymously. It contains no secrets — verified by listing its contents — the
code is public anyway, and CI scans both the filesystem and the built image with
Trivy before it is pushed. A private product would use ACR with a
managed-identity pull and pay the bill.

---

## "Why does the ID-qualified OIDC subject matter? Isn't it just a string?"

It is just a string, and which string it is determines what can authenticate as
my deploy identity.

The subject on `opsbridge-deploy`'s one federated credential is:

```
repo:Alhamwis@<ownerId>/OpsBridge365@<repoId>:environment:production
```

Two things are encoded there, and both cost a failed deploy to learn.

**`environment:production`, not `ref:refs/heads/main`.** An environment-gated job
presents a subject naming the *environment*; the branch does not appear at all. I
had created the credential for the branch, because that is what the workflow's
trigger says, and it reads correctly right up until Entra refuses it with
`AADSTS700213` — an error that mentions nothing about environments.

Be precise about what that buys, because this document used to overstate it.
Declaring an environment changes the OIDC subject; it does not by itself gate
anything. An environment is a surface that *can* carry protection rules, and for a
long time this one carried none — so "trust is pinned to an environment rather
than a branch" was a description of a mechanism, not of a control. As of
**2026-08-29** the repository is configured with branch protection on `main`
(pull request required, required status checks, no force push, no deletion) and
the `production` environment limited to the `main` branch. That is the configured
state; I have not yet watched a protection rule refuse a push, so I would present
it as configuration rather than as a tested control — the same distinction the
alert story below is about.

**The `@<ownerId>` and `@<repoId>` qualifiers.** GitHub's default subject for this
account carries numeric id suffixes on both the owner and the repository, and
`use_default` was already `true`, so there was nothing to normalise away — this
*is* the default. Entra has to match it character for character.

That is the one I would actually argue is interesting, because it is **rename-proof**.
A subject built from names silently follows a repository or account rename, and
worse, a name that gets freed and re-registered by someone else inherits a live
trust relationship pointing at my subscription. A subject built from immutable ids
cannot do either: rename the repo and the deploy fails loudly, register the freed
name and you match nothing. It is a stricter binding, and it is worth keeping
rather than working around.

The general principle: **when a trust boundary is a string comparison, the string
should be built from identifiers that cannot be reassigned.** The same reasoning
is why I hold exactly one federated credential rather than adding a `ref:` one
alongside "just in case" — an unused credential is a second trust path nobody is
checking, and if a future job drops the environment gate I want it to fail rather
than quietly succeed through a credential that was never meant to authorise it.

---

## "You wrote up a broken alert. Why is that worth putting in the repo?"

Because it is the only finding in this project that inspection could not have
produced, and because the failure mode it represents is the one that hides
longest.

**What happened.** I built a Log Analytics scheduled query rule for sync
failures, wired it to an action group, and then — rather than trusting it — broke
the job on purpose: set `ASSETS_LIST_ID` to an invalid value, ran the job, watched
it go to **Failed**, and restored the value immediately. Then I asked the alert
query what it saw.

**Zero hits.** Against a job that had genuinely just failed.

The application has exactly two failure statuses: `config_error` (exit 2) and
`graph_error` (exit 1). My query matched `config_error`. An invalid list id is
*present, well-formed configuration that Graph refuses*, so it fails on the
`graph_error` path. The query now covers both, plus `Traceback` and `CRITICAL` as
a catch-all, and returns **2 hits** against that same failure.

**Why it is worth documenting rather than quietly fixing.** Nothing about that
rule looked wrong. It was syntactically valid, pointed at the right workspace, had
a sensible severity and window, and matched a status string the application really
does emit. A code review would have passed it. A screenshot of it in a portfolio
would have looked like monitoring.

And the blast radius, had it shipped: a sync job failing every six hours, an asset
register going quietly stale, and a monitoring dashboard reporting no alerts —
which reads as health. **Silence from an untested alert is indistinguishable from
silence from a healthy system.**

Two rules I would take to a team:

1. **An alert you have not deliberately triggered is an assumption, not a
   control.** The gap between "an alert exists" and "an alert fires" is exactly
   the gap between a monitoring slide and monitoring.
2. **Enumerate every failure path the code can actually take, then match all of
   them** — and add a catch-all anyway, because the enumeration was right this
   time and might not be next time.

The honest extension of the same doubt: I tested this control. I have not
deliberately triggered every other control in this repository, so each of those
is a weaker claim, and [`SECURITY.md`](SECURITY.md) says so.

---

## "Why write `Unknown` instead of making a reasonable guess?"

Because a wrong value in an asset register is worse than an admitted gap, and the
reason is about who checks it afterwards. **Nobody audits a field that looks
filled in.** A row reading `Unknown` gets investigated; a row reading a plausible
name gets trusted, cited in a decision, and never questioned. The guess does not
just fail — it consumes the attention that would have caught it.

The rule shows up in four places, at increasing levels of stubbornness:

1. **Ambiguity resolves to nothing.** A device matches an Assets row by serial
   number first, then device name. **A key that matches more than one row matches
   nothing at all** — not the first row, not the best row. Two candidates means no
   answer.
2. **Unresolvable text fields get the literal string `Unknown`.** Not blank, not
   the previous value, not a default. `Unknown` is a value that says a sync ran
   and could not determine this.
3. **A date column that cannot be determined is left untouched.** `LastCheckIn`
   is a date, and there is no date that means "we don't know" — writing epoch or
   today's timestamp would be inventing a fact. So the field is omitted from the
   PATCH entirely, and the count of those appears in the job's JSON summary, so
   the gap is *visible* rather than merely absent.
4. **`sla_compliance_7d_pct` returns `null` on a zero denominator.** Not 0%, not
   100%. And a ticket resolved with no due date counts as resolved but is excluded
   from the denominator rather than assumed to have met its target — which would
   inflate the number in exactly the direction that flatters me.

**The proof is the run I would show** — captured 2026-08-18. The tenant had zero
devices, so the first cloud sync had nothing to match and all four assets stood at
`Unknown`. I created one Entra device with a registered owner and ran the job
again: one row got a real user and a real compliance status, three stayed
`Unknown`. One confident match, three honest gaps, from a single execution against
live data. The fourth rule has since demonstrated itself too: `/metrics` returns
`sla_compliance_7d_pct: null` today, because the seven-day window rolled past the
seeded resolutions and there is nothing to compute a percentage from.

**Where the rule costs something, and what I would change at scale.** "Ambiguous
matches nothing" is right at small scale and silently drops data at large scale —
at ten thousand devices, a naming collision means a quietly growing set of rows
that never update. So at 10× I would keep the rule and change its *reporting*:
push the unmatched and ambiguous sets to a dead-letter list or an alert rather
than counting them in a JSON summary nobody reads. The failure mode I would be
guarding against is a sync that matches less and less while continuing to report
success — which is the same shape as the alert defect above, and hides just as
long.

---

## "Talk me through the security model in thirty seconds."

Two app registrations, in two tenants. The one that deploys to Azure
authenticates with OIDC federation and **has no secret at all** — GitHub mints a
short-lived token whose subject is pinned to one repository and one *environment*,
by immutable numeric id. The one that calls Graph **has** a secret but **no Azure
RBAC whatsoever** — it lives in Key Vault, the containers read it through a managed
identity whose only permission anywhere in Azure is "read secrets from this one
vault", and it is never inlined into a container definition or emitted as a
deployment output.

So the identity with authority has no credential to leak, and the credential that
can leak has no authority in Azure. Graph permissions are read-only on users and
devices, and SharePoint write is `Sites.Selected`, scoped to a single site rather
than every site in the tenant.

Two more sentences if they want the current shape rather than the original one.
**The deployment identity holds Contributor and nothing more**, because the one
privileged step — creating the Key Vault role assignment — moved into a bootstrap
template a human runs once.

Two details worth having ready, because they are what separates a claim from a
control. First, the **order**: the split templates were deployed green *before*
the elevated grant was removed, not after, so there was never a window where the
documentation was ahead of reality. Second, it is **continuously asserted**, not
asserted once — every deployment runs a step that attempts to create a `Reader`
role assignment as the pipeline identity and fails the build if Azure permits
it. A privilege reduction nothing checks is a privilege reduction that quietly
comes back.
And **live tenant data is behind a bearer token**:
`/metrics` authenticates, rate limits and caches; `/healthz` and `/demo/metrics`
stay public because neither touches a tenant.

The line I would finish on: **when I try to read that secret myself, holding
Contributor on the resource group, Key Vault returns `ForbiddenByRbac`** —
re-verified 2026-08-29. Only the managed identity can read it. Least privilege is
easy to claim and hard to demonstrate, and the demonstration is a refusal aimed at
me.

Full version, including the gaps, in [`SECURITY.md`](SECURITY.md).

---

## "What was the hardest bug or the thing you got wrong first?"

Two, both recorded in
[`evidence/docker/build-and-run.md`](../evidence/docker/build-and-run.md) with the
failing output, captured 2026-08-16.

**`/healthz` depended on configuration.** It called `get_settings().app_version`,
which raises with an empty environment, which the app-wide handler turned into a
503 — so Docker's `HEALTHCHECK` would kill a container that was merely
unconfigured, and Container Apps would do the same in a restart loop. The fix was a
one-line fallback to `app.__version__`, but the lesson is the general one: **a
liveness probe must not depend on secrets.** `/metrics`, which genuinely needs
credentials, still returns 503 — the distinction is between "I am alive" and "I can
do my job".

**`pyproject.toml` declared a `README.md` that did not exist**, so `pip install .`
failed inside the Docker build. Trivial, but it only surfaced because I built the
container instead of assuming it would build. That is the argument for running the
thing rather than reasoning about it.

---

## "Did you use AI to build this?"

**I designed, deployed and verified the system, using Claude Code as an
engineering assistant. I can explain and reproduce every major architectural and
security decision.**

That is not a hedge, so here is what it means concretely for this repository.

**Every significant decision has a rejected alternative attached to it.** Container
Apps over Functions and AKS, GHCR over ACR, a bearer token over the platform's
built-in Entra authentication, a bootstrap template over a permanently privileged
pipeline identity, `null` over `0%` on a zero denominator. Each of those is in
this document with the reason it went that way and what it costs — and I can argue
the losing side of any of them, because I had to hold both sides to choose.

**The things that decided this design could not be produced by writing code.**
The OIDC subject is ID-qualified — that came out of a second `AADSTS700213` after
the obvious fix, against a real Entra tenant. `eastus` is forbidden by an Azure
for Students policy. Five resource providers were unregistered on a fresh
subscription. The failure alert did not fire, and I only know that because I broke
the job on purpose and asked the query what it had seen. None of those is
discoverable by reading a repository, and all of them are why the deployment
section of this project is the part I would want to be interviewed on.

**The corrections are the load-bearing evidence.** This repository shipped a
documented cold start of 714 ms and a claim that the Microsoft 365 subscription was
a paid plan rather than a trial. Both were wrong. I found them by re-measuring
rather than by re-reading: a genuine scale-from-zero probe returned 20.2 s, and the
subscription reports `isTrial: true` with a lifecycle date of 2026-09-16. Both are
corrected in public, with the old numbers named as wrong instead of quietly
deleted. A plausible sentence is cheap; a measurement with a date and a command
next to it is not, and that is the standard the whole repository is now held to.

**How to test the claim rather than take it.** Clone it and run
`pip install --require-hashes --no-deps -r requirements-dev.txt` then
`python -m pytest -q` — 106 tests, offline, no credentials. `curl` the two public
endpoints. Then ask me to change something: where the delta query would go and
what it would need to persist, what breaks the moment `maxReplicas` becomes 2
(the rate limiter's in-process counters), why the cache is unkeyed, why
authentication runs before rate limiting rather than after, or what happens to
`/metrics` if the trial lapses. Those answers are not retrievable from the
repository text; they are why the design is the shape it is.

The disclosure is in the README rather than left for someone to infer: the
project was designed, deployed and verified by its author with AI-assisted
development tools, and all architecture, security and operational claims are
backed by reproducible tests or captured cloud evidence.

---

## "Why should I believe your test count and your status table?"

Because most of it is re-runnable in under two seconds by you, and the parts that
are not carry the date they were taken.

```
$ python -m pytest -q
106 passed, 12 deselected

$ python -m pytest -m integration -q      # with tenant credentials, 2026-08-18
12 passed, 58 deselected in 10.06s
```

The integration line is a capture from **2026-08-18**, when the suite had 58
offline tests; today it has 106, so the deselected count would differ. It needs
tenant credentials to reproduce, and the tenant is a trial with a 2026-09-16
lifecycle date — after that, expect it not to be reproducible at all.

Two of the three endpoints are public, so `curl` settles the deployment claims
without asking me for anything, and `/metrics` refusing you with a 401 is itself a
check you can run. The pipeline claim is the
[run list on `main`](https://github.com/Alhamwis/OpsBridge365/actions/workflows/deploy.yml),
not an adjective and not a run id frozen in a document.

The general defence is better than any single number: **every figure in
[`STATUS.md`](STATUS.md) is a timestamped measurement with the command that
produced it next to it. Re-run them and expect drift.** Cold start was 20.2 s on
2026-08-29 and will not be exactly that for you. `/metrics` returned
`sla_compliance_7d_pct: null` that day because the window was empty, and it will
return a number again as soon as something is resolved. A document that promises
a frozen number is promising something a live system cannot deliver — which is
precisely how this file came to cite 714 ms long after it stopped being true.

And the parts that are *not* verifiable by you are stated as gaps rather than
omitted: the $0.00 spend was observed once, on 2026-08-18, and covers days rather
than a billing cycle; the end-to-end run was one user and one device; there is no
load or uptime figure and none is published from six probes a day; and the
teardown script has never been run against the live resource group.

The habit underneath it is `scripts/verify-opsbridge.ps1`, which reports anything
it could not check as **SKIP**, never PASS — so an unauthenticated run tells you
exactly what it did *not* verify. That rule is in the script's own documentation,
and it is the one I would bring to a team: the difference between "checked and
passed" and "could not check" is the whole value of a report.
