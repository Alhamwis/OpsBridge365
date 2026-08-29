# Security

Threat model and controls for the OpsBridge365 cloud layer.

> **Scope note.** A control that is designed and committed is not the same as a
> control that has been observed working, and this document marks which is
> which. Every claim carries either the date it was measured or the command that
> re-checks it.
>
> **Re-verified 2026-08-29 against the live deployment.** `GET /metrics` refuses
> an unauthenticated caller with **401**; `GET /demo/metrics` returns data
> labelled `"synthetic": true`; Key Vault denies the human operator with
> **`ForbiddenByRbac`**; the resource group holds 8 resources; gitleaks over the
> full history *with the allowlist disabled* reports **0 findings**;
> `ruff check .` is clean; 106 offline tests pass. The commands behind each of
> those are in [`STATUS.md`](STATUS.md).
>
> **Captured 2026-08-18 and not re-checked since.** The two app registrations
> read back from Entra, the `Sites.Selected` boundary probe, and HTTPS-only
> ingress — see [`evidence/security/posture.md`](../evidence/security/posture.md).
>
> **Captured 2026-08-16 and not re-checked since.** The container hardening run
> — see [`evidence/docker/build-and-run.md`](../evidence/docker/build-and-run.md).
> Note that the image has been rebuilt since, on a digest-pinned base.
>
> **Configured in this release and not independently verified.** The GitHub
> repository hardening in [§10](#10-repository-hardening-on-github). Those are
> settings on github.com; nothing in this repository can prove them, and no
> attempt has been made to violate one.
>
> Two controls are worth reading for what they cost rather than what they claim:
> the OIDC federation **failed twice before it worked** (§1), and the failure
> alert built on top of all this **would not have fired** on the first real
> failure it was tested against — see
> [`evidence/monitoring/alerting.md`](../evidence/monitoring/alerting.md).
>
> Anything not implemented at all is listed under [Gaps](#gaps--what-is-not-done).

---

## What is worth protecting

| Asset | Why an attacker wants it |
| --- | --- |
| The Graph app registration's **client secret** | It is app-only credentials to a whole tenant's users and devices. No user interaction, no MFA prompt |
| The **`/metrics` bearer token** | It stands between the internet and live tenant ticket counts. Whoever holds it can also make the service do Microsoft Graph work and keep a scale-to-zero container awake |
| The **Azure subscription / resource group** | Compute to abuse, resources to destroy, a bill to run up |
| The **SharePoint Assets and Tickets lists** | Write access means poisoning an asset register; read access means an org's device inventory |
| The **live ticket counts themselves** | Aggregates, not contents — but "how far behind is this service desk" is operational information about a real organisation |
| The **CI/CD pipeline** | A pipeline that can deploy is a pipeline that can deploy an attacker's image |
| The **container image and everything it is built from** | A public image that accidentally contains a credential is a credential published to the internet; a substituted base layer or dependency is code running with the container's identity |

---

## Controls

### 1. No stored cloud credentials — OIDC federation

`deploy.yml` authenticates to Azure with **workload identity federation**. There
is no client secret, no service-principal password, and no `creds:` JSON blob
anywhere in the workflow. GitHub mints a short-lived OIDC token scoped to this
repository — that is what `id-token: write` buys — the `Azure/login` action
exchanges it for an access token, and the token dies with the job.

**This works: the pipeline deploys to Azure through it**, with no stored Azure
credential of any kind. The first fully green run was `32115509179` on
**2026-08-18**; at the time of writing (2026-08-29) the most recent successful
deploy is run `32122134218` at commit `550f737`. Current state is the run list,
not a number pasted into a document:
<https://github.com/Alhamwis/OpsBridge365/actions/workflows/deploy.yml>.

The federated credential's `subject` pins trust to one repository and one
execution context. `opsbridge-deploy` holds **exactly one** federated credential,
and its subject is:

```
repo:Alhamwis@<ownerId>/OpsBridge365@<repoId>:environment:production
```

Two properties of that string are load-bearing, and each cost a failed deploy to
discover:

1. **`environment:production`, not `ref:refs/heads/main`.** An environment-gated
   job presents a subject naming the environment; the branch does not appear at
   all. Trust is pinned to a GitHub environment, so a workflow that drops the
   environment declaration stops authenticating rather than quietly continuing to
   work. The environment now also carries a protection rule limiting it to
   `main` — configured in this release, and described as configured state in
   [§10](#10-repository-hardening-on-github).
2. **The `@<ownerId>` and `@<repoId>` qualifiers.** GitHub's default subject for
   this account is **ID-qualified**, and `use_default: true` does not normalise
   that away, so Entra must match the ID-qualified string. That form is
   **rename-proof**: a subject built from names silently follows a repository or
   account rename and can be inherited by whoever registers the freed name next;
   one built from immutable ids cannot. It is a stronger binding, not a quirk to
   work around.

**This control has been exercised, and it refused a deploy — twice.** Both
attempts failed with `AADSTS700213: No matching federated identity record found
for presented assertion`, first on the ref-versus-environment mismatch, then on
the ID qualification. [`DEPLOYMENT.md`](DEPLOYMENT.md#things-that-will-bite-you)
documents both in full, because they are the single most likely thing to bite
someone reproducing this.

Worth naming plainly: this is what a narrow trust boundary looks like from the
inside. A credential scoped `repo:*` or issued as a client secret would have
deployed on the first attempt, and would have been the weaker configuration. Two
failed runs is what the narrowness costs, and it is the right price.

### 2. Two app registrations, split by blast radius — **implemented**

This is the central security decision, and merging the two "for convenience" would
undo it. Both app registrations exist, in two different tenants, and the table
below describes what they actually hold — not what they are designed to hold.

| | `opsbridge-deploy` | `opsbridge-graph` |
| --- | --- | --- |
| Tenant | The one that owns the Azure subscription | The Microsoft 365 tenant that owns the data |
| Authenticates with | One OIDC federated credential | A client secret |
| Has a stored password? | **No. Zero passwords, zero certificates** — verified by reading its credential list back (2026-08-18) | Yes — the only one in the system |
| Azure RBAC | Contributor, **resource-group scope only** — *plus* a standing Role Based Access Control Administrator assignment that [§2b](#2b-the-infrastructure-split--what-the-routine-identity-can-no-longer-do) has made unnecessary and that is still in place (measured 2026-08-29) | **None at all** |
| Graph permissions | **None** | Three application permissions, admin-consented; **zero delegated grants** |
| Used by | The `Azure/login` step in `deploy.yml` | The containers, via Key Vault |

**The property that matters is verified, not merely intended: the deploy identity
has no credential of any kind.** Not a weak one, not a rotated one — none. There
is nothing on that principal for an attacker to steal, phish or find in a log,
because the only way to authenticate as it is to be GitHub Actions running this
repository's `production` environment. The identity that has ARM authority has no
credential to leak; the credential that can leak has no ARM authority.

One merged app would mean a long-lived Graph secret attached to a principal that
can also deploy to the resource group — leak it and the attacker gets Azure, not
just Graph read access.

Until this release the deploy identity **needed** Role Based Access Control
Administrator, because `main.bicep` created the Key Vault Secrets User assignment
for the container identity and Contributor cannot create role assignments. That
role assignment now lives in `infra/bootstrap.bicep`, so the requirement is gone;
the assignment on the principal has not yet been removed. Both facts are stated
because a privilege that is no longer needed and is still held is exactly the
kind of thing a security document is supposed to say out loud.

Consent for `opsbridge-graph` was granted **programmatically**, by creating
`appRoleAssignments` on the service principal rather than by clicking through the
portal, and then **verified by reading the consent state back**. That matters for
two reasons: the grant is reproducible and reviewable as code rather than as a
remembered click, and reading it back is what distinguishes "consent was
requested" from "consent exists".

### 2a. The bootstrap app — a privilege escalation, documented and reversed

`Sites.Selected` with the `write` role **cannot create SharePoint lists.** It can
read and PATCH items on a granted site; creating the list schema in the first
place needs a site-level administrative permission. This was an open expectation
in an earlier revision of this document; it is now a finding.

Rather than raise the runtime identity's permissions to cover a one-time
provisioning step — the obvious and wrong shortcut — a separate throwaway app,
`opsbridge-bootstrap`, was registered with `Sites.FullControl.All`, used to create
the `Assets` and `Tickets` schema, and then **deleted**.

| | What happened |
| --- | --- |
| Who held `Sites.FullControl.All` | `opsbridge-bootstrap`, and only it |
| For how long | Long enough to create two list schemas |
| What holds it now | Nothing. The app registration is deleted |
| What the runtime identity ever held | `Sites.Selected` with `write` on one site. **It never held FullControl** |

The security property is the reversal, not the restraint. A tenant-wide SharePoint
write permission existed for a bounded window, was used for one purpose, and was
removed by deleting the principal that held it — which is stronger than removing
the permission, because it also removes the credential and the service principal.
The alternative shortcut would have left `Sites.FullControl.All` permanently
attached to the identity whose secret sits in Key Vault and runs on a cron, which
would have made the entire `Sites.Selected` argument in §4 decorative.

Stated as a rule: **a permission needed once should be held by a principal that
exists once.** The provisioning script is idempotent (a re-run reports 13 EXISTS /
0 CREATED), so re-creating a bootstrap app for a future schema change is a
deliberate, visible act rather than standing authority.

### 2b. The infrastructure split — what the routine identity can no longer do

The same rule, applied to the templates. The Bicep is now two files with two
different operators:

| | `infra/bootstrap.bicep` | `infra/main.bicep` |
| --- | --- | --- |
| Run by | A human, from a workstation, deliberately | GitHub Actions, on every push to `main` |
| How often | Once, plus a rotation | Every deploy |
| Creates | Key Vault, the user-assigned identity, the **Key Vault Secrets User role assignment**, and the two **secret values** | Log Analytics, the Container Apps environment, the `opsbridge-sync` Job, the `opsbridge-api` App |
| References | — | The vault and the identity as `existing` |
| Privilege it needs | Enough to create a role assignment | **Contributor** |
| Sees a secret value | Yes, once, as a `@secure()` parameter | **Never** |

What the routine deployment identity can no longer do, as a direct consequence:

- **It cannot create or change a role assignment.** `main.bicep` no longer
  declares one, so the pipeline does not need Role Based Access Control
  Administrator. A compromised pipeline therefore cannot grant itself — or
  anything else — data-plane access to the vault.
- **It cannot receive the Graph client secret, because it is never given one.**
  `GRAPH_CLIENT_SECRET` is no longer a GitHub secret and `main.bicep` no longer
  has a `clientSecret` parameter. The value moves from an operator's terminal to
  Key Vault once and never travels again.

What it still can do, stated so the split is not read as more than it is:
Contributor on the resource group can redeploy the API with a different image,
and that image runs as the identity that *does* hold Key Vault read access. The
split removes standing authority to hand out permissions and removes the
credential from GitHub. It does not make a compromised pipeline harmless — branch
protection and the environment rule in [§10](#10-repository-hardening-on-github)
are what stand in front of that, and a review of what is being deployed is what
stands in front of those.

`bootstrap.bicep` is a **prerequisite**, not an optional extra: deploying
`main.bicep` into a resource group that was never bootstrapped fails at the
`existing` lookup, which is the intended loud failure rather than a half-built
environment.

```bash
az deployment group create -g rg-opsbridge365 \
  --template-file infra/bootstrap.bicep \
  --parameters graphClientSecret=<value> metricsApiToken=<value>
```

Both secret parameters are optional and independent — each resource is guarded by
`if (!empty(...))` — so a rotation passes only the value being rotated and leaves
the other secret untouched. Prefer a parameters file to the form above: a shell
expands `graphClientSecret=<value>` before `exec`, so the plaintext becomes an
element of `az`'s command line and is readable in the process table.

### 3. Two secrets, both in Key Vault — **verified against the deployment**

There are exactly two secrets in the system, and neither is stored anywhere but
Key Vault:

| Secret | What it is | Delivered as |
| --- | --- | --- |
| `graph-client-secret` | The `opsbridge-graph` client secret | `AZURE_CLIENT_SECRET`, a `secretRef` |
| `metrics-api-token` | The bearer token that authenticates `GET /metrics` | `METRICS_API_TOKEN`, a `secretRef` |

Each was checked against the running resources rather than against the template
that declares it; the 2026-08-18 checks are in
[`evidence/security/posture.md`](../evidence/security/posture.md), and the Key
Vault denial was re-run on 2026-08-29.

| Check | Result |
| --- | --- |
| Container App secrets | **`keyVaultUrl` is set; there is no inline `value`** |
| How the container receives them | `secretRef`s, resolved at replica start by the user-assigned identity |
| Deployment outputs | Exactly `apiFqdn`, `identityPrincipalId`, `jobName`, `keyVaultName`, `logAnalyticsWorkspaceId` — **no secret**. `bootstrap.bicep` outputs the vault name, the identity, and the *names* of the secrets it manages |
| Reading a secret **as the human operator** | **`ForbiddenByRbac`** (re-verified 2026-08-29) |

**Key Vault denies the human operator.** Only the user-assigned identity
`opsbridge-id` holds Key Vault Secrets User on the vault. The operator who
created the vault, and who holds Contributor on the resource group, **cannot read
what is in it**.

That denial is the strongest single line in this document, because least
privilege is easy to assert and hard to demonstrate — and the demonstration is a
refusal aimed at yourself. It works because the vault is **RBAC-authorized rather
than access-policy based**: data-plane access is one explicit role assignment,
and nothing inherits it from the management plane. Contributor on a resource
group is broad, and it is tempting to assume it implies data access to
everything inside; it does not. Read the denial precisely: it proves the operator
cannot **read** a stored value. It is not a claim that nothing can be written to
the vault by other means.

The operational corollary is worth stating rather than hiding: an operator who
needs to rotate a secret must first grant themselves the role, visibly, as a role
assignment somebody can see. Privileged access that leaves a trace beats standing
authority that does not.

The rest of the control:

- Both values are `@secure()` Bicep parameters in `bootstrap.bicep` — ARM does
  not log them, does not echo them into deployment history, and does not return
  them from `az deployment group show`.
- **Neither value passes through GitHub.** `main.bicep` names the secrets and
  never sees them. The only values the deploy step hands to `az` are identifiers:
  a tenant id, a client id, a site id and two list ids. This is a narrower claim
  than the one this document used to make — "secrets reach the CLI through the
  step's `env:` block, never a command line" was misleading, because a shell
  expands `param="$VAR"` before `exec` and the expanded value is on `az`'s argv
  regardless of where it came from. The fix was to stop passing the credential,
  not to describe the old path more carefully.
- Access is a single role assignment of the built-in **Key Vault Secrets User**
  role (`4633458b-17de-408a-b874-0445c86b69e6`), **scoped to the vault** — not the
  resource group, not the subscription. The identity can read secret values and
  do nothing else anywhere in Azure.
- The vault has **soft delete enabled** (7-day retention), so a deleted secret or
  vault is recoverable rather than gone.
- **No secret is emitted as a deployment output.** The outputs are an FQDN, a
  vault name, a workspace id, a job name and a principal id — all safe to print,
  which matters because deployment outputs are readable by anyone with
  resource-group access.
- **Rotation** for either secret: re-run `bootstrap.bicep` with just that
  parameter (or set the secret directly, having granted yourself the data role),
  then restart the API revision. The containers resolve a versionless
  `keyVaultUrl` at replica start, so a running replica keeps the old value until
  it is replaced.

### 3a. `/metrics` — authenticated, rate limited, cached, and fail-closed

**This endpoint used to be public, and that was the largest hole in the system.**
It is worth being specific about what was open, because "unauthenticated
endpoint" understates it. One anonymous `GET /metrics` did all of this:

1. **Disclosed live tenant data.** Real open-ticket counts and a real SLA
   percentage for a real organisation. Aggregates, not ticket contents — but
   published to anyone who could type a URL.
2. **Amplified into Microsoft Graph.** Each request built a new MSAL application,
   acquired a token, and read a live SharePoint list. One cheap HTTP request
   became several upstream calls, so a `while true` loop was a Graph throttling
   attack against the tenant — and the scheduled sync job shares that tenant's
   throttling budget.
3. **Held a scale-to-zero container awake.** `minReplicas: 0` is the entire cost
   argument (see [`COST.md`](COST.md)). An endpoint anyone can poll is an
   endpoint anyone can use to keep the meter running.
4. **Was unbounded in time.** Individual Graph requests had timeouts; the
   aggregate operation did not, so a deeply paged or slow upstream could hold a
   request open on a single-replica container for as long as it liked.

Every one of those is now closed by a named control:

| Control | What it does | Where |
| --- | --- | --- |
| **Bearer authentication** | A token read from Key Vault, compared with `hmac.compare_digest` so the comparison does not leak how much of a guess was right. Wrong scheme, empty credential and a *prefix* of the real token are all 401. Token strength is an operator responsibility — generate with `openssl rand -base64 32`; the 32-character floor is recorded in the code as `MIN_TOKEN_LENGTH` but is not enforced at runtime | `app/security.py` |
| **Order: authenticate, then rate limit** | An unauthenticated flood cannot consume the rate-limit budget of a legitimate caller sharing an egress address | `app/security.py` |
| **Rate limit** | 30 requests per minute per caller, sliding window, `Retry-After` on the 429. Keyed on the left-most `X-Forwarded-For` entry, because Container Apps terminates TLS and `request.client` would otherwise be the ingress | `app/ratelimit.py` |
| **45-second cache with single-flight** | N callers inside the window cost **one** Graph read. Concurrent misses await the same in-flight fetch rather than each starting their own — a plain TTL cache still stampedes | `app/cache.py` |
| **25-second wall-clock deadline** | The whole refresh, not each request. Exceeding it is a 504, not a held-open connection | `app/main.py` |
| **Fail closed** | If `METRICS_API_TOKEN` is unset the endpoint returns **503** and serves nothing. A misconfiguration must never silently reopen the hole | `app/security.py` |
| **Generic errors, server-side audit** | Every refusal is logged with the caller fingerprint and a reason (`missing_bearer`, `invalid_token`, `throttled`); the token itself is never logged and never echoed | §7 |

**How this is checked, rather than asserted.** `tests/test_security.py` holds 15
offline tests covering each behaviour above — including that an unauthenticated
request never reaches Graph, that a 401 body does not disclose the expected
token, that unauthenticated requests do not consume the limit, that repeated
calls inside the cache window hit Graph once, and that a hung upstream becomes a
504. `tests/test_cache.py` and `tests/test_ratelimit.py` cover the primitives.
Live, `GET /metrics` without a token returned **401** on 2026-08-29;
`deploy.yml` fails the deployment if that check ever returns 200; and
`health.yml` re-runs it every 4 hours.

#### The trade-off, stated honestly

**A static bearer token is weaker than short-lived Entra tokens.** It does not
expire. Every caller holds the same copy, so the token identifies "somebody who
has the token", not a principal — the audit line records an address, not an
identity. Revocation is rotation: update the Key Vault secret and restart the
revision. Its confidentiality rests entirely on TLS in transit and on whoever
holds it storing it properly.

**Container Apps' built-in Entra authentication (EasyAuth) was considered and not
chosen**, for two reasons that are worth writing down because they are trade-offs
rather than oversights:

- It validates tokens in the platform, *above* the application process. None of
  the behaviours this endpoint actually needs — 401 on a bad token, 429 under
  load, cache-hit accounting — could then be proven by the offline test suite.
  The control would have been stronger and the evidence for it weaker.
- It requires another app registration in an institutional tenant that does not
  hand those out freely, and it makes the endpoint impossible to demonstrate
  without an interactive sign-in or an Azure CLI session.

**The upgrade path**, in the order it would be done: put Entra validation in
front of `/metrics` (either EasyAuth or in-process JWT validation against the
tenant's JWKS), keep the rate limiter and the cache exactly as they are since
neither depends on how the caller was authenticated, and demote the static token
to a break-glass path or delete it. Nothing in the current design blocks that —
the authentication is one FastAPI dependency, and it is the only thing that would
change.

**Why the demo does not need the token.** `GET /demo/metrics` is public and
returns fabricated numbers with `"synthetic": true` **in the body**, not only in
a header, so the label survives being pasted into a screenshot. It performs no
upstream call, so it cannot be used to amplify anything, and it means "show me
what the API returns" never requires handing out a credential.

### 4. Least-privilege Microsoft Graph permissions

Application permissions, admin-consented. What the code actually calls, and what
each permission is for:

| Permission | Needed for | Where |
| --- | --- | --- |
| `User.Read.All` | `GET /users` — to resolve a device's registered owner into a display name for `AssignedUser`. Also covers the `registeredOwners` expansion on the device call | `app/graph.py: list_users` |
| `Device.Read.All` | `GET /devices?$expand=registeredOwners` — device name, serial number, `isCompliant`, `approximateLastSignInDateTime` | `app/graph.py: list_devices` |
| `Sites.Selected` | Read the Tickets list, read and PATCH the Assets list — on **one** site only | `app/sharepoint.py` |

All three are **read** permissions except the SharePoint write, and none is a
`.ReadWrite.All` directory permission. The app cannot create a user, cannot modify
a device, and cannot see mail, files or Teams data.

**`Sites.Selected` is the one worth calling out.** `Sites.ReadWrite.All` would
grant write access to *every* SharePoint site in the tenant. `Sites.Selected`
grants nothing on its own: after consent, an admin authorizes the app on
individual sites via `POST /sites/{site-id}/permissions` with `"roles": ["write"]`.
The blast radius of the one stored Graph secret is then a single site, not a
tenant's entire SharePoint estate. That is the stricter choice and it is the one
specified.

#### What `Sites.Selected` does and does not hide

This is a non-obvious boundary and it is easy to overclaim, so here is what was
**measured** on 2026-08-18 against the live tenant with the app's own token
(`opsbridge-graph`, `Sites.Selected` + one `write` grant):

| Request | Result | What it means |
| --- | --- | --- |
| `GET /sites/{granted}` | **200** | The granted site resolves |
| `GET /sites/{granted}/lists` | **200**, 3 lists | Granted-site data is readable |
| `GET /sites/{granted}/lists/{assets}/items` | **200**, 4 items | The sync job's actual read path works |
| `GET /sites/{rootHost}` | **200** | **Metadata of an ungranted site is readable** |
| `GET /sites/{rootId}/lists` | **200**, **zero** lists | Reachable, but discloses nothing |
| `GET /sites/{rootId}/drive` | **403** `accessDenied` | Ungranted-site *data* is refused |
| `GET /sites/{rootId}/drive/root/children` | **403** `accessDenied` | Same |
| `GET /sites?search=*` | **403** `accessDenied` | No tenant-wide search |
| `GET /sites/getAllSites` | **403** `accessDenied` | No tenant-wide enumeration |

Read plainly: **`Sites.Selected` does not hide a site's existence or its basic
metadata.** For a site the app was never granted, `GET /sites/{hostname}` returns
200 with the site's id, `webUrl` and display name. That is expected Microsoft
behaviour, not a misconfiguration in this tenant, and it is not something a
per-site grant model was ever designed to prevent.

What the permission actually withholds — and what the security posture here
relies on — is two things:

1. **Data access.** Content on an ungranted site (`/drive`, drive children) is
   refused with `403 accessDenied`. `/lists` on the ungranted root site returns
   200 but discloses no lists, so there is nothing to read even where the call
   itself is permitted.
2. **Enumeration.** Both tenant-wide discovery paths — `/sites?search=*` and
   `/sites/getAllSites` — are refused with `403 accessDenied`. The app cannot
   build a list of sites to go after; it can only address a site whose id it was
   already given.

So the accurate claim is *"the app cannot read data on, or discover, any site
other than the one it was granted"* — **not** *"the app cannot see other sites."*
The latter is false, and stating it would be an overclaim that a reviewer with a
token could disprove in one request.

This is also why `tests/integration/test_live_graph.py` asserts denial on
`/sites/{rootId}/drive` and on `/sites?search=*` rather than on
`GET /sites/{hostname}`. An earlier version of that test asserted 403 on the
ungranted site's *metadata* and consequently **failed against a correctly
configured tenant** — the security property was real, the assertion was aimed at
the wrong surface. Each of those tests now carries a docstring saying so, and
there is a positive control (`test_granted_site_data_is_accessible`) because a
403 proves nothing unless the same call is shown to succeed where a grant exists.

**`Device.Read.All` is the directory permission, deliberately.** `app/graph.py:
list_devices` calls `GET /devices` — the directory device object — so that is the
permission the code needs. This service does not call Intune / Endpoint Manager
anywhere, and none of the three permissions above touches it. Reading Intune
device data is a possible future step (noted in
[`ARCHITECTURE.md`](ARCHITECTURE.md)); it would need the Intune managed-devices
permission *and* an Intune licence, neither of which is in scope.

> **Corrected 2026-08-16.** `docs/DEPLOYMENT.md` previously named the Intune
> managed-devices permission here, both in the runbook table and in the
> `az ad app permission add` loop. Anyone following it would have consented to a
> scope the code never uses while lacking the one it does, so `GET /devices`
> would have failed with `403 Authorization_RequestDenied` at runtime while the
> app looked correctly configured. Replaced with `Device.Read.All` throughout;
> the table above always matched the code.

**The caveat that used to be here has been resolved.** An earlier revision said
that `Sites.Selected` with `write` "may" be insufficient for
`scripts/provision_sharepoint.py` to create lists and columns, and stated it as an
expectation. It is now a fact: `write` covers the sync job's runtime needs (read
items, PATCH items) and **cannot create lists**. The list schema was created by
the throwaway `opsbridge-bootstrap` app described in [§2a](#2a-the-bootstrap-app--a-privilege-escalation-documented-and-reversed),
which was deleted afterwards. The runtime identity's permission set was not
widened to accommodate a one-time step.

### 5. Container hardening

The runtime properties below were verified by running the image — see
[`evidence/docker/build-and-run.md`](../evidence/docker/build-and-run.md),
captured 2026-08-16. The build-reproducibility properties are read from the
`Dockerfile` at HEAD.

- **Non-root.** A fixed uid/gid 10001 (`appuser`, `--no-create-home`,
  `/usr/sbin/nologin`). `docker run --rm opsbridge365:local id -u` returns
  `10001`, and `docker exec` on the live server container returns the same, so it
  is the server process's uid and not a one-off.
- **Read-only to the runtime user.** `touch /srv/probe` inside the container
  returns `Permission denied` — application files are owned by root, the process
  is not.
- **Base image pinned by digest.**
  `python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217`,
  with the human-readable tag kept as a comment. A tag is a moving target — this
  one advanced on 2026-08-27 — so a tag-only build is a different image tomorrow
  with no commit to point at.
- **Dependencies installed from a hash-pinned lock**, with
  `pip install --require-hashes --no-deps -r requirements.txt`. A substituted or
  tampered wheel fails the build instead of shipping. See
  [§8a](#8a-supply-chain--what-is-pinned-and-what-scans-it).
- **Multi-stage.** `build-essential` and the compiler toolchain stay in the
  builder stage and never ship. Test tooling is not installed at all: the runtime
  image never gets the `[dev]` extra, so no pytest, respx or ruff.
- **No credentials in the image.** No `.env`, no `ARG` or `ENV` carrying a
  secret — the only `ENV` values are `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`,
  `PATH` and pip behaviour flags. Confirmed by listing the image contents.
- **Minimal contents.** No `tests/`, `docs/`, `evidence/` or `.git` in the image.
- **`HEALTHCHECK` uses stdlib `urllib`** rather than adding `curl` — one less
  binary in the image and one less thing to abuse. It probes `/healthz`, which
  needs no token, which is why the probe still works now that `/metrics` is
  closed. Note what it is *not*: Azure Container Apps does not run a Docker
  `HEALTHCHECK`, and `main.bicep` declares no probes, so this directive is
  exercised by `docker run` locally and by nothing in Azure.
- **HTTPS only, observed.** Ingress sets `allowInsecure: false`, and plain
  `http://` against the live FQDN returned **301** to `https://` when measured on
  2026-08-18. That is measured behaviour, not configured intent.

### 6. No secrets in Git — and the ordering that guarantees it

The **first commit in this repository is `.gitignore` and nothing else**
(`50f92b7`, "gitignore committed before any secret can exist"). That ordering is
the control: `.env`, `.env.*` (with an explicit `!.env.example` exception),
`*.secret`, `secrets.json`, `*.pem`, `*.pfx`, `azure-credentials.json` and
`infra/main.parameters.json` were all ignored before a single line of code
existed, so there was never a window in which a credential could be staged by
accident. A secret scrubbed from a later commit is still in the history; one that
was never committable is not.

`.env.example` and `infra/main.parameters.example.json` are placeholders only.
`main.parameters.json` — the filled-in copy — is gitignored. Since the
[infrastructure split](#2b-the-infrastructure-split--what-the-routine-identity-can-no-longer-do)
the routine template has no secret parameter at all; the only place a secret
value is ever typed is a one-time `bootstrap.bicep` deployment, and a parameters
file is preferred there so the value never reaches a command line.

Test fixtures use obviously-fake values (`unit-test-value-not-a-credential`,
`test-token-not-a-real-jwt`, zero-padded GUIDs) so no test data resembles a real
credential. Seed rows in the provisioning script are visibly fictional —
`CONTOSO-*`, `SN-SAMPLE-*`, `INC-100x`, summaries prefixed `Sample -`.

**The detective control alongside it: gitleaks in CI.** `.gitignore`-first is
preventive and depends on the ignore list being complete; the scan does not.
Both `ci.yml` and `deploy.yml` run a `secret-scan` job (`gitleaks/gitleaks-action`,
pinned to a commit SHA) with three properties that are the whole point:

- **Full history, not the diff.** `actions/checkout` with `fetch-depth: 0`. A
  shallow clone only sees the tip, so a credential committed and then deleted a
  commit later would scan clean — and that is precisely the case where the
  credential is still live and still published.
- **A hard gate.** No `continue-on-error`. In `deploy.yml`, `build-and-push`
  declares `needs: [secret-scan, test, codeql]`, so a finding stops the run before
  an image is pushed to ghcr.io and therefore before anything can reach Azure.
- **A minimal allowlist: one block, seven exact values.** `.gitleaks.toml`
  extends the default ruleset (`useDefault = true`, nothing disabled) and
  allowlists **seven** values, each **by exact value, never by pattern**: four
  Azure built-in role definition GUIDs (Key Vault Secrets User, Contributor,
  Role Based Access Control Administrator, User Access Administrator) and the
  three Microsoft Graph app-role ids for `User.Read.All`, `Device.Read.All` and
  `Sites.Selected`. Every one is a fixed public identifier Microsoft publishes,
  identical in every tenant, granting nothing on its own — but each looks like a
  high-entropy string to the generic key rule. Matching by exact value means a
  real 36-character credential that merely *looks* like a GUID still fails the
  build. `.env.example` and `infra/main.parameters.example.json` are **not**
  allowlisted — they scan clean on their own merits and must keep being scanned.
  (The file is one `[allowlist]` block containing seven regexes; counting the
  block rather than the values is where "exactly one value" came from.)

**This runs on GitHub and passes.** In the 2026-08-18 run `32115509179` the
`secret scan (gitleaks)` job reported SUCCESS over the full history; `build and
push to ghcr.io` ran only because that and the test gate cleared, and `deploy to
Azure` ran only because the build did. Every artifact that has reached Azure
passed a full-history secret scan first.

Re-run locally on 2026-08-29, over the full history:

```bash
# As CI runs it. gitleaks resolves its config from --config, then
# $GITLEAKS_CONFIG, then <source>/.gitleaks.toml - so this picks up the
# project allowlist automatically.
gitleaks git . --log-opts="--all"
#   -> no leaks found
```

To prove the allowlist is a narrow exemption rather than a blanket bypass, run
it again with the project config genuinely out of the way. Note that simply
omitting `--config` does **not** do this: gitleaks still finds
`.gitleaks.toml` in the scanned directory. You have to point it somewhere else:

```bash
printf '[extend]\nuseDefault = true\n' > /tmp/no-allowlist.toml
gitleaks git . --log-opts="--all" -c /tmp/no-allowlist.toml
#   -> leaks found: 2
```

Both of those two are the thing the allowlist exists for, and nothing else:

| Rule | Where | What it actually matched |
| --- | --- | --- |
| `generic-api-key` | `docs/DEPLOYMENT.md` | `USER_ACCESS_ADMIN=18d7d88d-…` — the public Azure built-in role definition id for **User Access Administrator** |
| `generic-api-key` | `infra/main.bicep` | `keyVaultSecretsUserRoleId = '4633458b-…'` — the public Azure built-in role definition id for **Key Vault Secrets User** |

Both are fixed identifiers published by Microsoft, identical in every tenant on
earth, and they grant nothing on their own. The high-entropy rule cannot tell
them apart from a credential, which is exactly why they are allowlisted **by
exact value** rather than by pattern — a real 36-character secret that merely
looks like a GUID is still caught.

That is the useful result. "Zero findings either way" would have meant the
allowlist was doing nothing and could be deleted; two findings, both accounted
for, means it is carrying exactly the weight it claims to.

GitHub's own secret scanning and push protection are now enabled as well, which
is the preventive half this section used to list as missing — see
[§10](#10-repository-hardening-on-github).

A separate manual disclosure review ran before the repository was made public:
zero real tenant, subscription, app, site or list identifiers in the working tree
**or in any commit in the history**. That review is why identifiers appear nowhere
in these documents — names and roles only.

### 7. The application does not leak configuration to callers

Every failure on `/metrics` is logged in full server-side and returned as a
generic message:

| Status | Body | When |
| --- | --- | --- |
| 401 | `{"detail": "Authentication required."}` | Missing, malformed or wrong bearer token — the same body either way, so the response does not say whether a token was recognised |
| 429 | `{"detail": "Too many requests."}` plus `Retry-After` | Rate limit exceeded |
| 502 | `{"detail": "Upstream authentication failed."}` / `"Upstream Microsoft Graph request failed."` | Graph auth or request failure |
| 503 | `{"detail": "Service configuration is incomplete."}` | Missing configuration, including a missing `METRICS_API_TOKEN` |
| 504 | `{"detail": "Upstream request timed out."}` | The 25-second deadline was exceeded |

No environment variable name, tenant id or secret ever reaches an HTTP caller —
proven in the container evidence, where the server log names the missing
variables and the HTTP response does not. A test pins that a 401 body does not
disclose the expected token.

Audit lines are server-side only and carry no secret:
`metrics_auth caller=<fingerprint> outcome=denied reason=invalid_token`. The
fingerprint is the left-most `X-Forwarded-For` entry truncated to 64 characters —
enough to bucket a caller and correlate a burst, never returned in a response.

Graph error bodies are truncated to 500 characters and reduced to `code: message`
before being logged, so an upstream response cannot flood the log. One honest
limit: when an upstream failure is *not* JSON — an intermediary error page, for
instance — the truncated body is logged as received rather than filtered. That is
a log-side exposure, not a response-side one, and it is listed under
[Gaps](#gaps--what-is-not-done).

The one piece of configuration any response does echo is deliberate:
`/healthz` returns the `APP_VERSION` value as `version`, so a caller can tell
which build answered. It is set by the deployment, not by a caller, and it is the
only such value.

### 8. Pipeline hygiene

- `ci.yml` runs on pull requests with **`permissions: contents: read`** at the
  workflow level and no registry login, no cloud credentials and no deploy step.
  A pull request from a fork cannot touch GHCR or Azure, because the token it
  gets cannot. Two jobs ask for more and say why: CodeQL needs
  `security-events: write` to upload its results, and dependency review needs
  `pull-requests: write` to comment a failure onto the pull request.
- **Permissions are declared per job**, and that is what is actually in effect: a
  job-level `permissions:` block replaces the workflow-level grant rather than
  intersecting with it. In `deploy.yml` the grants are `contents: read` for
  secret-scan, test and deploy; `security-events: write` for CodeQL;
  `packages: write` only for the job that pushes the image; `packages: read` for
  the image scan; and `id-token: write` only for the deploy job that mints the
  OIDC token. The workflow-level block is the default for any job that forgets to
  declare its own.
- **Every gate is hard.** `secret-scan`, `test` (ruff **and** pytest) and
  `codeql` all gate `build-and-push`; `scan-image` (Trivy plus SBOM) gates
  `deploy`. `ruff` used to carry `continue-on-error: true`, which meant a lint
  failure was reported and then ignored all the way to a production deployment;
  that is removed and lint now blocks the image build and the deploy.
- The deploy job additionally requires `github.ref == 'refs/heads/main'` **and**
  the `production` GitHub environment, so a manual dispatch from a side branch can
  build an image but cannot deploy it.
- **The deploy verifies itself, and one of the checks is a security regression
  guard.** After `az deployment group create` the job requires `GET /healthz` to
  return 200 (retried for ~3 minutes, because a cold start is expected),
  `GET /demo/metrics` to return `synthetic: true`, the sync job to be
  schedule-triggered, and **`GET /metrics` with no token to return 401**. A 200
  there fails the deploy: if authentication is ever removed, the pipeline that
  removed it is the thing that reports it.
- Concurrency on deploy is `cancel-in-progress: false` — killing a job midway
  through `az deployment group create` would leave the resource group
  half-updated.
- The image is deployed by **immutable sha tag**, not `:latest`.
- `az logout` runs with `if: always()`.
- `health.yml` — the only workflow that observes the service between deploys —
  holds `contents: read`, uses no secrets, calls `/healthz` and never `/metrics`,
  and **never redeploys or remediates**. A failed probe is left red, because
  automatic recovery would erase the signal the workflow exists to capture. It
  also re-checks that `/demo/metrics` is synthetic and that `/metrics` still
  refuses an unauthenticated caller.

### 8a. Supply chain — what is pinned, and what scans it

Everything the build consumes is pinned to something immutable, and something
automated proposes the updates. Pinning without automation is how a project ends
up two years behind; automation without pinning is how it ends up shipping
whatever was published this morning.

| Layer | Pinned to | Enforced by |
| --- | --- | --- |
| GitHub Actions | A full 40-character commit SHA, with the version in a trailing comment | Every `uses:` in `ci.yml`, `deploy.yml`, `health.yml` |
| Python dependencies | A fully resolved, **hash-pinned** lock — `requirements.txt` (31 runtime packages) and `requirements-dev.txt` (39 with dev), generated from `pyproject.toml` for linux / py3.12 | `pip install --require-hashes --no-deps -r ...` in CI and in the Dockerfile |
| Container base image | `python:3.12-slim@sha256:09f7da…5217` | `Dockerfile`, both stages |
| Deployed image | The immutable `:${GITHUB_SHA}` tag, not `:latest` | `deploy.yml` |

**Why SHA pins for actions.** A tag is a movable ref. Whoever controls an
action's repository can repoint `@v4` at new code, and in this pipeline that code
would run in a job holding `packages: write` or `id-token: write`. A SHA cannot
be moved.

**Regenerating the locks** — after a Dependabot bump, or a change to
`pyproject.toml`:

```bash
uv pip compile pyproject.toml --python-version 3.12 --python-platform linux \
  --generate-hashes --no-header -o requirements.txt
uv pip compile pyproject.toml --extra dev --python-version 3.12 --python-platform linux \
  --generate-hashes --no-header -o requirements-dev.txt
```

The scanners, and exactly what each one blocks:

- **CodeQL** (`python`, `security-extended` query suite) runs on pull requests
  and on `main`, and gates the image build.
- **Trivy, filesystem scan** on pull requests, over the repository including the
  locks.
- **Trivy, image scan** in `deploy.yml`, against the exact digest just published
  rather than against a rebuild of it.
- **The Trivy policy, stated once and applied in both places:** a **CRITICAL or
  HIGH finding that has a fix available fails the build** and the image is never
  deployed (`severity: CRITICAL,HIGH`, `ignore-unfixed: true`, `exit-code: 1`).
  Findings with no fix available are reported and do not block. That asymmetry is
  deliberate: failing a build on a vulnerability nobody can fix yet does not make
  anything safer, it just teaches people to write ignore files.
- **SBOM.** An SPDX-JSON bill of materials is generated for the published image
  and uploaded as a build artifact (90-day retention). The build step also emits
  provenance and SBOM attestations alongside the image.
- **Dependabot**, weekly, for pip, docker and github-actions, with updates
  **grouped** rather than filed per package — eleven separate pull requests on a
  Monday morning get rubber-stamped, and rubber-stamping a dependency bump is the
  failure this is meant to prevent. Every one of those pull requests runs the full
  `ci` gate.
- **Dependency review** on pull requests, failing on `high`, so a newly
  introduced vulnerable dependency is caught at the diff rather than at the scan.

What this does not claim: none of these scanners has yet caught and blocked a
real vulnerability in this repository. They are configured and the policy above
is what they would enforce. A clean scan is also a statement about *known* CVEs
at the moment it ran, which is why the weekly bumps matter as much as the gates.

### 9. Cost as a safety control

Denial of wallet is a real threat against a student subscription, and the
guardrails against it are the same ones that bound abuse:

- `replicaTimeout: 1800` and `replicaRetryLimit: 1` on the sync job mean a hung
  or failing Graph call cannot bill for more than 30 minutes or retry in a loop.
- `maxReplicas: 1` on the API caps what a request flood can spend.
- The authentication, the 30-per-minute limit and the 45-second cache on
  `/metrics` are the other half. Before them, an anonymous loop could hold the
  container awake indefinitely; now an unauthenticated caller gets a 401 that
  costs one wake-up at most, and an authenticated one costs at most one Graph
  read per 45 seconds however fast they poll.
- The scheduled health check probes every 4 hours rather than every 5 minutes,
  for the same reason: a frequent monitor would hold a scale-to-zero container
  awake and quietly convert a $0 service into a billed one — measuring a system
  it had itself changed.

Cold start from zero replicas measured **20.2 s** on 2026-08-29 (warm: 248 ms).
That latency is the price of `minReplicas: 0`, and it is the reason the deploy
job retries `/healthz` for about three minutes rather than probing once. See
[`COST.md`](COST.md).

### 10. Repository hardening on GitHub

Configured in this release, on 2026-08-29. **Described as configured state, not
as a verified control:** these are settings on github.com, nothing in this
repository can prove them, and no attempt has been made to violate one.

| Setting | Configured as |
| --- | --- |
| Branch protection on `main` | Pull request required; required status checks must pass; no force push; no deletion |
| `production` environment | Deployment restricted to `main` |
| Secret scanning | Enabled |
| Push protection | Enabled — a recognised credential is refused at `git push`, before it reaches GitHub at all, which is strictly better than the gitleaks job that catches it after the fact |

Two consequences worth knowing. First, the required status checks are matched by
**job name**, so renaming a job in `ci.yml` silently disables its
branch-protection rule — the names are an interface, not a label. Second, this is
what turns `environment: production` from a string that shapes the OIDC subject
into a rule that also restricts what can deploy; §1 previously leaned on the
first property alone.

To re-check: repository **Settings → Branches**, **Settings → Environments** and
**Settings → Code security**, or
`gh api repos/Alhamwis/OpsBridge365/branches/main/protection`.

---

## What an attacker would target, and what stops them

| Attack | What stops it |
| --- | --- |
| **Pull the published image and extract credentials** | There are none. Configuration arrives as environment variables at runtime; no `.env` or credential-bearing `ARG`/`ENV` is in the image (verified). The package `ghcr.io/alhamwis/opsbridge365` is public, and the image is designed to be safe when public |
| **Read live ticket counts from `/metrics`** | **401** without a bearer token; verified live on 2026-08-29, re-checked on every deploy and every 4 hours by `health.yml` |
| **Loop `/metrics` to amplify into Graph, or to hold the container awake** | 30 requests/minute per caller, then 429 with `Retry-After`; a 45-second cache with single-flight coalescing means N callers cost at most one Graph read per window; a 25-second deadline bounds any single refresh |
| **Steal the `/metrics` bearer token** | It exists only in Key Vault and in the container's environment at runtime. What it buys is five aggregate numbers — no ticket contents, no Graph access, no ARM. Rotation is a Key Vault update plus a revision restart |
| **Remove the authentication and hope nobody notices** | The deploy job fails if unauthenticated `/metrics` returns 200, and the 4-hourly health workflow fails the same way |
| **Steal the Graph client secret from the repo** | It was never committable — `.gitignore` was the first commit. It exists only in Key Vault, and since the infrastructure split it is not a GitHub secret either |
| **Steal it from the deployed app's configuration** | **Verified:** the app's secret carries a `keyVaultUrl` and no inline value, and the container gets a `secretRef`. Reading the value needs the managed identity, which only Container Apps can assume — the human operator gets `ForbiddenByRbac` |
| **Steal it from deployment outputs or logs** | `@secure()` keeps it out of ARM history; no output emits a secret; the routine deployment never receives one |
| **Use the leaked Graph secret to attack Azure** | That app registration holds **no Azure RBAC at all**. It gets Graph read access plus write to one SharePoint site — nothing in ARM |
| **Use the leaked Graph secret to attack the wider tenant** | Permissions are read-only on users and devices; SharePoint write is `Sites.Selected`, scoped to one site |
| **Steal the Azure deploy credential** | There isn't one — verified, not assumed: `opsbridge-deploy` holds zero passwords and zero certificates. OIDC tokens are minted per run and expire with the job |
| **Make the pipeline grant itself access to the vault** | Creating a role assignment needs a privilege the routine identity does not have, and `main.bicep` no longer contains one |
| **Move a tag on a third-party action to run code in a privileged job** | Every action is pinned to a full commit SHA; a tag cannot be repointed into this pipeline |
| **Ship a malicious or substituted dependency** | Hash-pinned locks installed with `--require-hashes`; a digest-pinned base image; Trivy on the filesystem and on the published image; dependency review on pull requests |
| **Open a malicious PR to run code with secrets** | `ci.yml` has `contents: read` and no secrets, no registry login, no cloud login |
| **Push to a side branch to deploy a backdoored image** | Deploy requires `refs/heads/main` **and** the `production` environment. The single federated credential's subject is `repo:Alhamwis@<ownerId>/OpsBridge365@<repoId>:environment:production` — a token from any other context is refused by Entra, which is exactly how the first two deploy attempts failed. Branch protection on `main` and the environment's `main` restriction are configured on top ([§10](#10-repository-hardening-on-github)) |
| **Escalate inside the container** | Non-root uid 10001, no writable application filesystem, no shell tooling beyond the base image, no compiler |
| **Enumerate configuration through error messages** | Responses are generic; nothing echoes a variable name, tenant id or secret. A 401 is identical whether the token was missing or wrong |
| **Throttle or DoS the API to run up a bill** | `maxReplicas: 1`, the rate limit and the cache, plus the Container Apps free grant; job timeout capped at 30 minutes |
| **Poison the asset register via ambiguous data** | Ambiguous match keys match nothing; unresolvable values are written as `"Unknown"`, never guessed |

---

## Gaps — what is *not* done

Stated plainly, because a security document that only lists wins is marketing.
This list is meant to be complete as of **2026-08-29**.

- **`/metrics` is guarded by a static bearer token.** It does not expire, every
  caller holds the same copy, it identifies "somebody with the token" rather than
  a principal, and revocation means rotating it. Short-lived Entra tokens are the
  stronger answer; the reasoning and the upgrade path are in
  [§3a](#3a-metrics--authenticated-rate-limited-cached-and-fail-closed).
- **Rate limiting is per replica.** The limiter is in-process, so it counts one
  container's traffic. That is the whole fleet at `maxReplicas: 1`, and it stops
  being the whole fleet the moment the service scales out; there is no shared
  store. The module docstring carries the same warning.
- **Key Vault allows public network access.** `publicNetworkAccess: Enabled` with
  `networkAcls.defaultAction: Allow`. Authorization is still one RBAC assignment —
  reachability is not access — but a private endpoint and a vault firewall are the
  production answer, and neither is in the free tier.
- **Teardown has never been exercised.** `destroy-cloud.ps1` is written and
  preflight-checked but has not been run against the live resource group, so
  "the blast radius is one resource group" is a design property that has not been
  demonstrated by deleting it.
- **The deploy identity still holds Role Based Access Control Administrator** at
  resource-group scope (measured 2026-08-29), even though
  [§2b](#2b-the-infrastructure-split--what-the-routine-identity-can-no-longer-do)
  removed the need for it. Until that assignment is removed, the routine identity
  is more privileged than the template it deploys requires — Contributor is the
  correct standing role.
- **The interactive API documentation is public.** `/docs`, `/redoc` and
  `/openapi.json` are served unauthenticated: FastAPI's defaults are not
  overridden. They describe three endpoints and echo the deployed version; they
  expose no tenant data, and `/metrics` still refuses without a token. Convenient
  for a portfolio demo, wrong for a production service, and one constructor
  argument away from being closed.
- **A non-JSON upstream error body is logged truncated but unfiltered.** Graph
  failures are normally reduced to `code: message`; when the response is not JSON
  the first 500 characters are logged as received. Nothing reaches an HTTP
  caller, but it is a log-side exposure that has not been closed.
- **The GitHub repository hardening is configured, not tested.** Nobody has tried
  to force-push to `main`, deploy from a side branch, or push a test credential to
  confirm push protection blocks it. [§10](#10-repository-hardening-on-github)
  says what was configured and how to re-read it; that is the honest limit of the
  claim.
- **No scanner in the supply chain has yet blocked a real finding here.** CodeQL,
  Trivy and dependency review are wired in with a stated policy. The policy is
  evidence of intent, and the run history will become evidence of effect.
- **A control was shipped broken, and testing is the only reason it is not still
  broken.** The Log Analytics failure alert matched `config_error` only, while
  the application's other failure status is `graph_error` — and the real failure
  emitted the second one. Against a genuinely failed job the alert query returned
  **0 hits**. It was syntactically valid, pointed at the right workspace, had a
  sensible severity and window, and matched a status string the application
  really does emit; reading it would never have caught it. The corrected query
  returns 2 hits against that same failure. **An untested alert is an assumption,
  not a control** — and the same doubt applies to every control in this document
  that has not been deliberately triggered. See
  [`evidence/monitoring/alerting.md`](../evidence/monitoring/alerting.md).
- **The controls are verified, not endurance-tested.** They were checked against a
  deployment first stood up on 2026-08-18 — eleven days old at the time of
  writing — with one user, one device and a handful of requests. Nothing here has
  met a hostile request pattern, a credential rotation performed in anger, a
  privilege review, or a real incident.
