# Interview notes

Questions this project invites, and honest answers. Where an answer is a design
intention rather than something observed running, it says so — nothing here has
been exercised in Azure.

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
4xx and malformed JSON. It has never met the real Graph API. Real throttling
behaviour at volume, and how often Graph actually sends `Retry-After` versus
expecting you to back off blindly, is something I would expect to learn in the
first week of running it.

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

And operationally: alerting on job failure (designed, never deployed), a dead
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

## "Why isn't it deployed?"

Because the accounts do not exist yet, and every one of them needs an interactive
human: a student subscription with identity verification, my own Entra tenant
(portal-only), an E5 trial with a payment method on file, `gh auth login`,
`az login` with MFA, and a global-admin consent click. They are listed as Phase 1
in [`DEPLOYMENT.md`](DEPLOYMENT.md) precisely because they are forms rather than
engineering.

What that means for what I can claim: the container and the application are
verified by running them. The template is verified to compile. The workflows and
the cloud design are reviewed but unexercised. I would rather say that than let
someone assume a green pipeline exists.

**The follow-up worth pre-empting** — *"so how do I know the cloud part works?"* —
has an honest answer: you do not, and neither do I. What you can assess is whether
the template is coherent, whether the identity split is sound, whether the failure
modes were thought about, and whether I can tell you precisely which parts are
unproven. I would rather be trusted for the last one.

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

## "Talk me through the security model in thirty seconds."

Two app registrations. The one that deploys to Azure authenticates with OIDC
federation and **has no secret at all** — GitHub mints a short-lived token scoped
to one repo and one ref. The one that calls Graph **has** a secret but **no Azure
RBAC whatsoever** — it lives in Key Vault, the containers read it through a managed
identity whose only permission anywhere in Azure is "read secrets from this one
vault", and it is never inlined into a container definition or emitted as a
deployment output.

So the identity with authority has no credential to leak, and the credential that
can leak has no authority in Azure. Graph permissions are read-only on users and
devices, and SharePoint write is `Sites.Selected`, scoped to a single site rather
than every site in the tenant.

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

Both are re-runnable in under two seconds, and I would rather you check.

The state file in the repo says 56 tests; the suite is 57 and I have re-run it. I
left the discrepancy documented rather than quietly fixing the number, because a
build-tracking file drifting from reality is exactly the kind of small dishonesty
that compounds — and because `scripts/verify-opsbridge.ps1` exists for the same
reason. It reports anything it could not check as **SKIP**, never PASS, so an
unauthenticated run tells you what it did *not* verify. That rule is in the
script's own documentation, and it is the habit I would bring to a team.
