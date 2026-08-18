# Interview notes

Questions this project invites, and honest answers. Where an answer is a design
intention rather than something observed running, it says so. Most of it is now
observed: the system is deployed in Azure, the pipeline deploys it, and the
numbers quoted below were measured rather than estimated.

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
became many small event handlers rather than two containers.

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
4xx and malformed JSON. The client itself has now met the real Graph API — twelve
integration tests and a cloud sync job run against a live tenant — but at a scale
where **nothing throttled**. So the happy path is proven against Microsoft and the
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
7. **Raise `maxReplicas` and add caching to `/metrics`.** Every call currently
   reads the whole Tickets list. At real traffic that is both slow and a throttling
   risk; a short TTL cache would absorb almost all of it, since SLA counts do not
   need to be accurate to the second.

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
- **Authentication on `/metrics`.** It is public and unauthenticated. It exposes
  counts and a percentage — no ticket contents, no personal data, no identifiers.
  Acceptable for a portfolio demo, not for production. Entra auth in front of
  ingress is the fix and it is a known gap, not an oversight.
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
- **Dependency scanning, SBOM.** Not implemented — no Dependabot, no CodeQL, no
  Trivy image scan, and dependencies are floor-pinned rather than lock-filed.
  The next thing I would add. (Secret scanning *is* implemented: gitleaks runs
  over the full git history in both workflows and hard-gates the image build.
  GitHub push protection is still off, because it needs the repo to exist first.)

---

## "Did you actually deploy it, or is this a repo full of YAML?"

Deployed, running, and measured. GitHub Actions run `32115509179` was green on all
four jobs — tests, gitleaks over the full history, image build and push, and
`deploy to Azure` — through OIDC federation with no stored Azure credential. The
API answers at
`https://opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io`, the
sync job has run on its cron in Azure and written a live SharePoint list, and the
observed spend is $0.00 against a $20 budget.

**It did not work first time, and that is the part worth asking about.** Three
runs failed for three genuinely different reasons — an OIDC subject that turned
out to be environment-scoped, then a subject that turned out to be ID-qualified,
then a subscription policy that forbids `eastus`, plus five unregistered resource
providers on top. `bicep build` returned zero diagnostics through every one of
them. The template was never the problem.

The general point I would make from it: **a template that compiles tells you
nothing about whether your subscription will accept it.** Every one of those
failures lived in the gap between a repository and one specific real
subscription — an identity provider's default claim format, a policy assignment,
a subscription's initial provider state. None of them is discoverable by reading
code, and all four are written up in
[`DEPLOYMENT.md` § Things that will bite you](DEPLOYMENT.md#things-that-will-bite-you).

**What I still would not claim.** The cost figure covers hours, not a billing
cycle. The end-to-end run was one user and one device. There is no load,
throughput or uptime measurement. Those are in the README's gaps table, and I
would rather point at them than have someone find them.

---

## "Why two Entra tenants? That looks like over-engineering."

It is the opposite — it is a workaround for a permission I cannot get, made
explicit. Microsoft Graph *application* permissions need tenant admin consent. My
institution will not grant a student's app registration `User.Read.All` and
`Device.Read.All`, and it should not. The only way to demonstrate app-only Graph
access is to be the admin of the tenant you consent in, so I create one.

The consequence is stated in `ARCHITECTURE.md` rather than hidden: the sync writes
to a SharePoint site in *my* tenant, not the institutional one, because
cross-tenant writes need consent from the same closed door. The site id and list
ids are configuration, so the same image deploys against any tenant that grants
consent — which is how a vendor ships this anyway.

**And then it turned into a security property.** Once the tenants were separate,
the two identities had to be separate too, and they landed in different
directories: `opsbridge-deploy` lives in the school tenant that owns the Azure
subscription, and `opsbridge-graph` lives in the Microsoft 365 tenant that owns
the data. That split is what makes the sentence "the identity with Azure
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

The trade is that the image is public. It contains no secrets — verified by listing
its contents — and the code is public anyway. A private product would use ACR with
a managed-identity pull and pay the bill.

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
`AADSTS700213` — an error that mentions nothing about environments. The security
value of getting it right: trust is pinned to a GitHub environment, which is a
thing with protection rules and required reviewers attached, rather than to a
branch name that anyone with push access can create.

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

**The proof is the run I would show.** The tenant had zero devices, so the first
cloud sync had nothing to match and all four assets stood at `Unknown`. I created
one Entra device with a registered owner and ran the job again: one row got a real
user and a real compliance status, three stayed `Unknown`. One confident match,
three honest gaps, from a single execution against live data.

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

The line I would finish on: **when I try to read that secret myself, holding
Contributor on the resource group, Key Vault returns `ForbiddenByRbac`.** Only the
managed identity can read it. Least privilege is easy to claim and hard to
demonstrate, and the demonstration is a refusal aimed at me.

Full version, including the gaps, in [`SECURITY.md`](SECURITY.md).

---

## "What was the hardest bug or the thing you got wrong first?"

Two, both recorded in `evidence/docker/build-and-run.md` with the failing output.

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

## "Why should I believe your test count and your status table?"

Because most of it is re-runnable in under two seconds by you, and the parts that
are not are pinned to things you can look up.

```
$ python -m pytest -q
58 passed, 12 deselected

$ python -m pytest -m integration -q      # with tenant credentials
12 passed, 58 deselected in 10.06s
```

The API is public, so `curl` settles the deployment claims without asking me for
anything. The pipeline claim is a run id — `32115509179` — not an adjective. The
status table quotes measured values (714 ms cold, 143 ms warm, the exact `/metrics`
payload, the exact job summary) rather than adjectives, and every row points at a
file under `evidence/` that says what was run and what came back.

And the parts that are *not* verifiable by you are stated as gaps rather than
omitted: the $0.00 spend covers hours rather than a billing cycle, the end-to-end
run was one user and one device, there is no load or uptime figure, and the
teardown script has never been run against the live resource group.

The habit underneath it is `scripts/verify-opsbridge.ps1`, which reports anything
it could not check as **SKIP**, never PASS — so an unauthenticated run tells you
exactly what it did *not* verify. That rule is in the script's own documentation,
and it is the one I would bring to a team: the difference between "checked and
passed" and "could not check" is the whole value of a report.
