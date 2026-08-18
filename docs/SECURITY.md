# Security

Threat model and controls for the OpsBridge365 cloud layer.

> **Scope note.** Everything below describes controls that are **implemented in
> the repository** — in the Bicep template, the workflows, the Dockerfile and the
> code. Nothing has been exercised against a live **Azure subscription**, because
> none exists yet. The **Microsoft 365 tenant side is different**: the Graph
> permissions and the `Sites.Selected` boundary have now been probed directly with
> the app's own token, and the measured results are in
> [What `Sites.Selected` does and does not hide](#what-sitesselected-does-and-does-not-hide).
> A control that is designed and committed is not the same as a control that has
> been observed working, and this document marks which is which. Anything not
> implemented is listed under [Gaps](#gaps-what-is-not-done).

---

## What is worth protecting

| Asset | Why an attacker wants it |
| --- | --- |
| The Graph app registration's **client secret** | It is app-only credentials to a whole tenant's users and devices. No user interaction, no MFA prompt |
| The **Azure subscription / resource group** | Compute to abuse, resources to destroy, a bill to run up |
| The **SharePoint Assets and Tickets lists** | Write access means poisoning an asset register; read access means an org's device inventory |
| The **CI/CD pipeline** | A pipeline that can deploy is a pipeline that can deploy an attacker's image |
| The **container image** | A public image that accidentally contains a credential is a credential published to the internet |

---

## Controls

### 1. No stored cloud credentials — OIDC federation

`deploy.yml` authenticates to Azure with **workload identity federation**. There
is no client secret, no service-principal password, and no `creds:` JSON blob
anywhere in the workflow. GitHub mints a short-lived OIDC token scoped to this
repository — that is what `id-token: write` buys — `azure/login@v2` exchanges it
for an access token, and the token dies with the job.

The federated credential's `subject` pins trust to one repository and one ref
(`repo:OWNER/OpsBridge365:ref:refs/heads/main`, plus one for
`environment:production`). A mismatch fails as `AADSTS70021: No matching federated
identity record found` — which is the failure mode you want, because it proves the
trust really is that narrow.

### 2. Two app registrations, split by blast radius

This is the central security decision, and merging the two "for convenience" would
undo it.

| | `opsbridge-deploy` | `opsbridge-graph` |
| --- | --- | --- |
| Authenticates with | OIDC federated credential | A client secret |
| Has a stored password? | **No. Never** | Yes — the only one in the system |
| Azure RBAC | Contributor + User Access Administrator, **resource-group scope only** | **None at all** |
| Graph permissions | **None** | Application permissions only |
| Used by | `azure/login@v2` | The containers, via Key Vault |

The identity that has ARM authority has no credential to leak. The credential that
can leak has no ARM authority. One merged app would mean a long-lived Graph secret
attached to a principal that can also deploy to the resource group — leak it and
the attacker gets Azure, not just Graph read access.

Note the deploy identity needs **User Access Administrator** (or the narrower
Role Based Access Control Administrator) only because `main.bicep` creates the
Key Vault role assignment for the container identity. That is a real privilege and
it is worth knowing it is there; it is scoped to one resource group, never the
subscription.

### 3. The one secret lives in Key Vault

- `clientSecret` is a `@secure()` Bicep parameter — ARM does not log it, does not
  echo it into deployment history, and does not return it from
  `az deployment group show`.
- It is stored as `graph-client-secret` and **never appears in a container
  definition**. Both workloads declare it as a Key Vault *reference* bound to the
  user-assigned managed identity; the platform fetches the value at replica start.
- Access is a single role assignment of the built-in **Key Vault Secrets User**
  role (`4633458b-17de-408a-b874-0445c86b69e6`), **scoped to the vault** — not the
  resource group, not the subscription. The identity can read secret values and
  do nothing else anywhere in Azure.
- The vault is **RBAC-authorized**, not access-policy based, so the entire
  authorization story is one role assignment that shows up in
  `az role assignment list`.
- **No secret is emitted as a deployment output.** The outputs are an FQDN, a
  vault name, a workspace id, a job name and a principal id — all safe to print,
  which matters because deployment outputs are readable by anyone with
  resource-group access.
- In the workflow, secrets reach the Azure CLI through the step's `env:` block —
  never interpolated into a command line. No `set -x` anywhere near a secret;
  `set -euo pipefail` only.

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
The blast radius of the one stored secret is then a single site, not a tenant's
entire SharePoint estate. That is the stricter choice and it is the one specified.

#### What `Sites.Selected` does and does not hide

This is a non-obvious boundary and it is easy to overclaim, so here is what was
**measured** against the live tenant with the app's own token (`opsbridge-graph`,
`Sites.Selected` + one `write` grant):

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

One honest caveat remains:

- **`Sites.Selected` with the `write` role covers the sync job's runtime needs**
  (read items, PATCH items). `scripts/provision_sharepoint.py` additionally
  *creates lists and columns*, which may require a higher site role (`manage` or
  `fullControl`) or a one-time run under an admin context. This has not been
  tested against a real tenant, so it is stated as an expectation, not a fact.

### 5. Container hardening

Verified by actually running the image — see
[`evidence/docker/build-and-run.md`](../evidence/docker/build-and-run.md):

- **Non-root.** A fixed uid/gid 10001 (`appuser`, `--no-create-home`,
  `/usr/sbin/nologin`). `docker run --rm opsbridge365:local id -u` returns
  `10001`, and `docker exec` on the live server container returns the same, so it
  is the server process's uid and not a one-off.
- **Read-only to the runtime user.** `touch /srv/probe` inside the container
  returns `Permission denied` — application files are owned by root, the process
  is not.
- **Multi-stage.** `build-essential` and the compiler toolchain stay in the
  builder stage and never ship. Test tooling is not installed at all: the runtime
  image gets `pip install .`, never `.[dev]`, so no pytest, respx or ruff.
- **No credentials in the image.** No `.env`, no `ARG` or `ENV` carrying a
  secret — the only `ENV` values are `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`,
  `PATH` and pip behaviour flags. Confirmed by listing the image contents.
- **Minimal contents.** No `tests/`, `docs/`, `evidence/` or `.git` in the image;
  `.dockerignore` keeps the build context under a kilobyte.
- **`HEALTHCHECK` uses stdlib `urllib`** rather than adding `curl` — one less
  binary in the image and one less thing to abuse.
- **HTTPS only.** Ingress sets `allowInsecure: false`; plain HTTP is redirected,
  never served.

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
`main.parameters.json` — the filled-in copy — is gitignored, and the recommended
practice is to leave `clientSecret` out of it entirely and pass it on the command
line.

Test fixtures use obviously-fake values (`unit-test-value-not-a-credential`,
`test-token-not-a-real-jwt`, zero-padded GUIDs) so no test data resembles a real
credential. Seed rows in the provisioning script are visibly fictional —
`CONTOSO-*`, `SN-SAMPLE-*`, `INC-100x`, summaries prefixed `Sample -`.

**The detective control alongside it: gitleaks in CI.** `.gitignore`-first is
preventive and depends on the ignore list being complete; the scan does not.
Both `ci.yml` and `deploy.yml` run a `secret-scan` job
(`gitleaks/gitleaks-action@v2`) with three properties that are the whole point:

- **Full history, not the diff.** `actions/checkout@v4` with `fetch-depth: 0`. A
  shallow clone only sees the tip, so a credential committed and then deleted a
  commit later would scan clean — and that is precisely the case where the
  credential is still live and still published.
- **A hard gate.** No `continue-on-error`. In `deploy.yml`, `build-and-push`
  declares `needs: [secret-scan, test]`, so a finding stops the run before an
  image is pushed to ghcr.io and therefore before anything can reach Azure.
- **A minimal allowlist.** `.gitleaks.toml` extends the default ruleset
  (`useDefault = true`, nothing disabled) and allowlists exactly one value: the
  Azure built-in role definition GUID for *Key Vault Secrets User* in
  `infra/main.bicep`, which is a fixed public identifier Microsoft publishes and
  is identical in every tenant. It is matched by exact value, so any other
  high-entropy string still fails the build. `.env.example` and
  `infra/main.parameters.example.json` are **not** allowlisted — they scan clean
  on their own merits and must keep being scanned.

Run against this repository's full history (gitleaks v8.30.1, 6 commits), the
only finding is that role GUID; with the allowlist in place the scan is clean.

### 7. The application does not leak configuration to callers

`/metrics` failures are logged in full server-side and returned as generic
messages: `503 {"detail": "Service configuration is incomplete."}` for missing
configuration, `502 {"detail": "Upstream authentication failed."}` for a Graph auth
failure. No environment variable name, tenant id or secret ever reaches an HTTP
caller — proven in the container evidence, where the server log names the missing
variables and the HTTP response does not.

Graph error bodies are truncated to 500 characters and reduced to `code: message`
before being logged, so an upstream response cannot flood or poison the log.

### 8. Pipeline hygiene

- `ci.yml` runs on pull requests with **`permissions: contents: read`** and no
  registry login, no cloud credentials, no deploy step. A pull request from a fork
  cannot touch GHCR or Azure, because the token it gets cannot.
- `deploy.yml` grants exactly three permissions and each is load-bearing:
  `contents: read` (checkout), `packages: write` (GHCR push), `id-token: write`
  (mint the OIDC token).
- The deploy job additionally requires `github.ref == 'refs/heads/main'` **and**
  the `production` GitHub environment, so a manual dispatch from a side branch can
  build an image but cannot deploy it.
- `secret-scan` **and** `test` both gate `build-and-push`, which gates `deploy`.
  The secret scan and the tests are hard gates; ruff is `continue-on-error` and
  advisory. The `secret-scan` job declares its own `permissions: contents: read`
  and needs nothing else — it reads the repository and that is all.
- Concurrency on deploy is `cancel-in-progress: false` — killing a job midway
  through `az deployment group create` would leave the resource group
  half-updated.
- Every action is pinned to a major version tag (`@v4`, `@v5`, `@v6`); nothing
  tracks `@master`.
- The image is deployed by **immutable sha tag**, not `:latest`.
- `az logout` runs with `if: always()`.

### 9. Cost as a safety control

`replicaTimeout: 1800` and `replicaRetryLimit: 1` on the sync job mean a hung or
failing Graph call cannot bill for more than 30 minutes or retry in a loop.
`maxReplicas: 1` on the API caps what a request flood can spend. Denial of wallet
is a real threat against a student subscription, and these are the guardrails
against it — see [`COST.md`](COST.md).

---

## What an attacker would target, and what stops them

| Attack | What stops it |
| --- | --- |
| **Pull the public image and extract credentials** | There are none. Configuration arrives as environment variables at runtime; no `.env` or credential-bearing `ARG`/`ENV` is in the image (verified) |
| **Steal the Graph client secret from the repo** | It was never committable — `.gitignore` was the first commit. It exists only in a GitHub Actions secret and in Key Vault |
| **Steal it from the deployed app's configuration** | It is a Key Vault reference, not a stored value. Reading it needs the managed identity, which only Container Apps can assume |
| **Steal it from deployment outputs or logs** | `@secure()` keeps it out of ARM history; no output emits a secret; no workflow step echoes one |
| **Use the leaked Graph secret to attack Azure** | That app registration holds **no Azure RBAC at all**. It gets Graph read access plus write to one SharePoint site — nothing in ARM |
| **Use the leaked Graph secret to attack the wider tenant** | Permissions are read-only on users and devices; SharePoint write is `Sites.Selected`, scoped to one site |
| **Steal the Azure deploy credential** | There isn't one. OIDC tokens are minted per run and expire with the job |
| **Open a malicious PR to run code with secrets** | `ci.yml` has `contents: read` and no secrets, no registry login, no cloud login |
| **Push to a side branch to deploy a backdoored image** | Deploy requires `refs/heads/main` **and** the `production` environment; the federated credential's subject is pinned to those |
| **Escalate inside the container** | Non-root uid 10001, no writable application filesystem, no shell tooling beyond the base image, no compiler |
| **Hit `/metrics` to enumerate configuration** | Responses are generic; nothing echoes a variable name, tenant id or secret |
| **Throttle or DoS the API to run up a bill** | `maxReplicas: 1`, plus the Container Apps free grant; job timeout capped at 30 minutes |
| **Poison the asset register via ambiguous data** | Ambiguous match keys match nothing; unresolvable values are written as `"Unknown"`, never guessed |

---

## Gaps — what is *not* done

Stated plainly, because a security document that only lists wins is marketing.

- **GitHub's own push protection is not enabled** — it cannot be until the
  repository exists on GitHub. Repo-side secret scanning *is* now in the pipeline
  (see below), but push protection blocks a secret before it is ever committed,
  which is strictly better, and it is a two-click setting to turn on once the
  repo is pushed.
- **No dependency or container scanning.** No Dependabot, no CodeQL, no Trivy
  image scan. Dependencies are floor-pinned (`>=`) rather than lock-filed, so a
  build is not byte-reproducible.
- **`/metrics` is unauthenticated** and behind public ingress. It exposes ticket
  *counts* and an SLA percentage — no ticket contents, no personal data, no
  identifiers — which is an acceptable trade for a portfolio demo and would not be
  for a real deployment. Container Apps ingress can be restricted to the
  environment, or Entra auth put in front of it.
- **Key Vault allows public network access** with `defaultAction: Allow`. Private
  endpoints and a firewall are the production answer; both cost money and neither
  is in the free tier.
- **No control has been verified in Azure.** Every claim about OIDC, Key Vault
  references, role scoping and ingress is a claim about the committed template and
  workflow, verified by reading and by `bicep build`, not by a deployment. The
  container and application controls *have* been verified by running them locally.
