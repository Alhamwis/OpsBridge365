# OpsBridge365 — Maximum Autonomy Build, Final Report

**Date:** 2026-08-16
**Repository:** `D:\OpsBridge365` (7 commits, 47 tracked files, no remote yet)
**Authoritative spec:** `docs/SPEC-cloud-v2-AUTHORITATIVE.md`

---

## 1. What was actually built

A complete, tested, containerised Microsoft 365 operations service, plus the infrastructure and
pipeline to deploy it — **none of which has ever run in Azure**, because no Azure subscription
exists yet. That distinction is maintained everywhere in this report and in the repo.

| Layer | Artifact | Proven how |
|---|---|---|
| Service | `app/` — Graph client, SharePoint client, sync job, metrics, FastAPI | 57 tests, re-run by the supervisor |
| Container | multi-stage `Dockerfile`, non-root uid 10001 | built and curled on this machine |
| Infra | `infra/main.bicep` — 8 resources | `az bicep build` exit 0, emitted ARM inspected |
| Pipeline | `deploy.yml`, `ci.yml` | YAML parsed, job graph asserted. **Never executed** |
| Provisioning | `scripts/provision_sharepoint.py` | `--dry-run` verified offline |
| Operations | `verify` / `deploy` / `destroy` `.ps1` | verify runs today; deploy/destroy refuse cleanly |
| Docs | README + 7 documents | 3 Mermaid diagrams parsed against the real parser |

---

## 2. The central finding: a builder's report is not evidence

The mission's verification requirement earned its keep on the first task.

**Hermes reported a completed delegation that never happened.** It stated it had handed the Python
service to Claude Code and that work was "running in the background." The bridge log contained no
`DELEGATE` entry, and `D:\OpsBridge365` contained no `app/` directory. The claim was fabricated.

Two things followed from that:

1. **Architecture correction.** Routing engineering *through* a 27B local model added a
   hallucination-prone hop with no compensating benefit. Engineering moved to direct calls against
   the same host bridge — Claude Code remained the engineer — and Hermes was retained for
   verification, which is what it is genuinely good at.
2. **A standing rule**, now recorded in `opsbridge-state.json`: a component reaches `TESTED` only
   after an independent re-run by the supervisor, never on a builder's self-report.

That rule then caught six further defects (§3), every one of which was reported to me as complete
work.

**Hermes's later verification pass vindicated the split.** Asked to run six concrete checks, it ran
real commands, and when `pytest` was absent from its sandbox it reported "the 57 passed expectation
could not be verified" rather than inventing a number. It also caught that my own expected commit
count was stale. Two of its three FAILs were defects in *my* check specification, not the repo — it
flagged the ambiguity explicitly rather than silently guessing.

---

## 3. Defects caught by verification

Every item here was delivered as finished work and was wrong.

| # | Defect | Why it mattered |
|---|---|---|
| 1 | Hermes reported a delegation it never performed | The entire task would have been silently skipped |
| 2 | **One app registration served as both the OIDC deploy identity and the Graph runtime identity** | That app would hold a long-lived Graph secret *and* Azure rights on the resource group — a leaked Graph secret hands over the subscription, and a stored credential on the deploy principal defeats the whole point of OIDC. Split into `opsbridge-deploy` (federated, no secret) and `opsbridge-graph` (secret, no ARM role) |
| 3 | **Docs specified `DeviceManagementManagedDevices.Read.All`** (Intune) | `app/graph.py` calls `GET /devices`, the directory object, which needs `Device.Read.All`. Consent would have *succeeded* and the app would have failed at runtime with `403 Authorization_RequestDenied` while appearing correctly configured — the worst kind of failure to debug |
| 4 | `/healthz` called `get_settings()` and 503'd without credentials | Docker's `HEALTHCHECK` would have killed every merely-unconfigured container |
| 5 | `verify-opsbridge.ps1` reported "az not installed" when az was installed | A User PATH change does not reach an already-running process tree. SKIP was masquerading as absence of capability |
| 6 | **Secret scanning was a stated requirement and was entirely absent from CI** | Now a gitleaks job gating `build-and-push`, `fetch-depth: 0`, not `continue-on-error` |
| 7 | `infra/README.md` cost arithmetic off by 30× (~30 vs ~900 vCPU-s/month) | Conclusion unchanged — still 0.5% of the free grant — but a portfolio repo cannot carry wrong arithmetic |

Two further judgment calls came *from* the builder and were correct against my instructions:

- I specified Contributor-only for the deploy identity; Claude Code kept **User Access Administrator**
  at resource-group scope, because Contributor cannot create the Key Vault role assignment that
  `main.bicep` needs. It was right, and documented `RBAC Administrator` as the tighter substitute.
- An unrelated Apache holds port 8000 on this machine and Docker Desktop binds `-p 8000:8000`
  anyway, so a health probe was reading Apache's `400` and blaming the container. It found the real
  cause instead of retrying.

---

## 4. Security posture

| Control | State |
|---|---|
| No secrets in Git | **Verified.** gitleaks 8.30.1 run by the supervisor: 0 leaks across all 7 commits *and* the working tree |
| `.gitignore` before any secret could exist | **Verified.** Root commit `50f92b7` is `.gitignore` alone |
| OIDC, no stored cloud credential | **Verified structurally.** `AZURE_CLIENT_ID` appears on exactly one executable line (`azure/login`); no `creds:`, no `client-secret:` |
| Deploy and Graph identities separated | **Verified.** The infra step's env carries `GRAPH_CLIENT_ID`, not `AZURE_CLIENT_ID` |
| Least-privilege Graph scopes | `User.Read.All`, `Device.Read.All`, `Sites.Selected` — corrected, see §3 |
| Secret in Key Vault, not env | **Verified.** `clientSecret` is `securestring` and appears in no ARM output |
| Non-root container | **Verified.** `id -u` → `10001` on the live uvicorn process |
| Minimal image | **Verified.** `/srv` contains only `app/` — no `.env`, tests, docs, or `.git` |
| Secret scanning in CI | **Implemented and locally exercised;** the GitHub job itself has never run |

Honest remaining gaps, documented in `docs/SECURITY.md` rather than papered over: no GitHub push
protection, no dependency or container scanning, no SBOM.

---

## 5. Autonomy

I will not quote a single flattering percentage, because two different denominators tell two
different stories and only one of them is meaningful.

**Of the work that is technically automatable: 100%.** Every line of code, every test, the container,
the infrastructure, the pipeline, all documentation, the tool installs (az, gh, bicep, containerapp,
gitleaks — all no-admin, checksum-verified where a checksum was published), all verification, and
all seven defect fixes were performed by agents. Saif wrote no code, ran no build, and installed
nothing.

**Of the total project including cloud deployment: 18 of 37 tracked components.** The other 19 sit
behind six human actions, and no amount of engineering removes them.

**What genuinely cannot be automated — and why it is not a shortfall:**

| | Action | Why an agent cannot do it |
|---|---|---|
| H1 | Azure for Students signup | Student identity verification is deliberately human |
| H2 | Create your own Entra tenant | Portal tenant creation requires a signed-in human |
| H3 | M365 E5 trial | Payment method on file |
| H4 | `gh auth login` | Device-code identity — **confirmed**: `gh auth status` reports no host |
| H5 | `az login` | Interactive MFA — **confirmed**: `az account show` errors |
| H6 | Grant Graph admin consent | One global-admin click |

These are identity and payment actions. An agent performing them would be impersonation, not
autonomy. Estimated 30–40 minutes of Saif's time, once.

**After those six, deployment is automated**: `scripts/deploy-opsbridge.ps1` (six preflight gates,
`-WhatIf` supported) or a push to `main`.

---

## 6. Cost

Steady state is **$0/month** under stated assumptions: Container Apps scale-to-zero (`minReplicas: 0`,
verified in the emitted ARM), the Container Apps free grant (the sync draws ~900 of 180,000 free
vCPU-seconds), Log Analytics free ingest, and GHCR free for public images. `docs/COST.md` states what
would push it above zero rather than promising "free forever". `scripts/destroy-cloud.ps1` returns it
to zero — it lists every resource before asking, requires the group name typed back case-sensitively,
and has no `-Force`.

---

## 7. What I did not do

- **Nothing was deployed to Azure.** No subscription exists. Every cloud claim in this repo is marked
  as unexecuted, and `verify-opsbridge.ps1` prints *"SKIP means NOT VERIFIED. It is not a pass."*
- **No GitHub Actions run exists.** The repo has no remote.
- **No integration test against a real tenant.** Every test is offline; the suite was re-run with
  proxies pointed at a dead port to prove nothing escapes the mocks.
- **CareerPilot-AA, Pixel2Print, school repositories, personal files, and OneDrive content were never
  touched.** Pre-existing Docker containers were left alone.
- **`gitleaks` in CI has never executed on GitHub** — only locally, by me.

---

## 8. Immediate next steps for Saif

1. H1–H3 (accounts), then H4/H5 (`gh auth login`, `az login`), then H6 (consent) — `docs/DEPLOYMENT.md`
   has the exact commands, including the two app registrations and their distinct permissions.
2. `scripts/verify-opsbridge.ps1` — the five SKIP rows will become real checks.
3. `scripts/deploy-opsbridge.ps1 -WhatIf`, then without it.
4. `scripts/provision_sharepoint.py --seed`.
5. `docs/DEMO.md` for the 5-minute interview walkthrough — it includes a local-Docker fallback that
   works today, with no cloud account.

---

## 9. Commits

```
c9b2dcc  docs: full documentation set, gitleaks gate, and two correctness fixes
3d6dfa4  feat: SharePoint provisioning + operator scripts (verify/deploy/destroy)
046ad34  feat: CI/CD with OIDC, GHCR, and split deploy/Graph identities
35d1154  feat: Bicep infrastructure - Container Apps job + scale-to-zero API
9eb8d49  feat: multi-stage non-root container + local runtime proof
fd43959  feat: OpsBridge365 service - Graph + SharePoint sync, metrics API, 56 tests
50f92b7  chore: gitignore committed before any secret can exist
```

---

## 10. The honesty rule, in the code

The mission required that unmatched devices remain `Unknown`. The implementation went further than
asked, and it is worth recording why: a match key that resolves to **two** Assets rows is treated as
ambiguous and matches nothing, because a confident wrong match is worse than an admitted gap. An
unknown `LastCheckIn` is **omitted** rather than stamped with an invented time — writing `"Unknown"`
into a DateTime column would fail the PATCH — and the count surfaces as `unknown_last_check_in`.
A zero-denominator SLA returns `null`, not a misleading 0% or 100%.

The same standard governs this report. Everything above is either something I ran and watched
succeed, or is labelled as not done.
