# OpsBridge365 Cloud Layer - Free Tier v2 (AUTHORITATIVE)

> This document is the authoritative cloud-layer design. Where it conflicts with
> SPEC-cloud-v1-historical.md, **v2 wins**.

OpsBridge 365 — Cloud Layer
Free-Tier Edition (v2) · Azure + Docker + CI/CD · Days 11–14
Replaces the v1 cloud-layer addendum. Same skills proven, $0 architecture: ghcr.io instead of ACR, Container Apps Jobs with scale-to-zero instead of always-on, and a clean two-tenant split.
What changed from v1 — and why it reads BETTER
GitHub Container Registry (ghcr.io) replaces Azure Container Registry. Free on a public repo, kills the only recurring cost. Pulling a public image into Container Apps needs no registry credentials at all.
The sync becomes a Container Apps JOB on a cron trigger, not an always-on app with a scheduler inside. It spins up, syncs, exits, costs nothing while idle — inside the monthly free grant (180,000 vCPU-seconds / 360,000 GiB-seconds per subscription).
The /metrics API scales to zero. A second Container App, HTTP-triggered, min replicas 0: it wakes when curled, sleeps when idle. Ignore v1’s "min replicas 1" — that line is what cost money.
Two tenants, deliberately. Service desk (Days 1–10) stays in your TCC tenant — standard connectors only, free forever, no trial clock. The cloud layer runs in a tenant YOU own, because Graph application permissions need admin consent TCC will never grant you.
Every one of these is also an interview answer. "Why a Job instead of an always-on container?" Because the workload is periodic and scale-to-zero is free. "Why ghcr over ACR?" Public portfolio, public image, zero cost. Choosing the cheap architecture on purpose — and saying why — reads as engineering judgment, not budget desperation.
Architecture (what the README diagram shows)
git push (public repo) → GitHub Actions (CI/CD, free)
  └─ docker build → ghcr.io (free public registry)
       └─ deploy → Azure Container Apps (free grant)
            ├─ JOB: opsbridge-sync — cron, run-and-exit
            │    ├─ auth:   Entra app reg (secret in Key Vault)
            │    ├─ pulls:  Microsoft Graph — users + devices
            │    └─ writes: SharePoint Assets list (LastCheckIn, ...)
            ├─ APP: opsbridge-api — HTTP, scale 0→1
            │    └─ /healthz + /metrics (live SLA stats)
            └─ logs → Log Analytics (5 GB/mo free)
The two-tenant split
TCC tenant — permanent home: SharePoint lists, Power Apps, all four flows, Teams, Power BI Desktop. The whole Days 1–10 service desk. No trial clock, nothing expires.
Your own tenant — 30-day burst: create a free Entra tenant in the Azure portal (permanent, you are global admin — gives app registrations, users, devices, Graph). Then start a Microsoft 365 E5 trial ON that tenant only when Day 11 begins: the trial adds SharePoint, and its 30 days comfortably covers Days 11–14 with room to spare.
The sync writes to SharePoint in YOUR tenant, not TCC’s — cross-tenant writes would need TCC admin consent, which is the exact door that is closed. So Day 11 includes a 30-minute step: recreate the Assets list (same columns, same seed rows) in a site on your trial tenant. The README states this plainly: same schema, deployable against any tenant that grants consent. That is how real vendors ship.
Card-on-file caveat: the E5 trial asks for a payment method (it does not charge if you cancel); Azure for Students does not ask at all. If putting a card on file is a hard no, tell me and I will restructure the sync to write to a free target instead (for example, Azure Table Storage) — the Docker/Azure/CI-CD skills demonstrated are identical.
When the trial dies, the portfolio does not. The repo, the Bicep template, the green pipeline runs, the screenshots, and the log output are the evidence — none of that expires. Days 11–14 are a burst: do them in one push and capture everything.

Day 10.5 — one-evening setup (do during Days 1–10)
Azure for Students (azure.microsoft.com/free/students) with your @my.tccd.edu address — $100 credit, 12 months, no credit card.
GitHub Student Developer Pack (education.github.com/pack) with the same address — free, worth it regardless.
Create your own Entra tenant in the Azure portal (Manage tenants → Create). Free, permanent, you are global admin. Do NOT start the E5 trial yet — the 30-day clock should start on Day 11, not today.
Install locally: Docker Desktop (free for students/personal), Azure CLI, Python 3.12, VS Code. Confirm: docker run hello-world.
Optional: ask TCC (Julie Hester would know) whether anything under Azure Dev Tools for Teaching is still active — the old program is discontinued and Azure for Students is the intended replacement, so check but do not plan around it.
Done today when: az login works, Docker runs, your own Entra tenant exists, and the E5 trial remains unstarted.
Watch out: This is the item that blocks Day 11 for a week if skipped. It is 30–45 minutes. Do it early.
Day 11 — Trial on, service running locally in Docker
Start the M365 E5 trial on your tenant. Set a calendar reminder for day 25 to harvest any remaining screenshots before expiry.
In the trial tenant: create a SharePoint site, recreate the Assets list (same columns as the spec), seed the same 15–20 devices. Add 2–3 test users and register a device or two so Graph has something real to return.
Entra app registration: client credentials, application permissions User.Read.All, Device.Read.All, Sites.ReadWrite.All (or Sites.Selected scoped to the one site — the stricter choice, and say so in the README). Grant admin consent — you are the admin now.
FastAPI project: /healthz; a sync module (MSAL auth → pull users + devices from Graph → PATCH matching Assets items: LastCheckIn, AssignedUser, ComplianceStatus; unmatched devices stay Unknown — honest data).
Write the sync as a run-and-exit entrypoint (python -m app.sync) — the container Job runs it and stops. No scheduler library needed; the cron lives in Azure.
Dockerfile: multi-stage, slim runtime, non-root user, HEALTHCHECK on /healthz. Build and run locally; watch a real list item update.
Done today when: A container on your laptop syncs Graph data into your own tenant’s Assets list, screenshotted from both sides.
Watch out: .env holds the client secret and .gitignore holds .env BEFORE the first commit — the repo is public from birth. A leaked tenant secret in a portfolio repo is the one mistake a hiring manager will hold against you.
Day 12 — Into Azure, as code, at $0
Push the image to ghcr.io (docker login ghcr.io with a GitHub token, docker push). Mark the package public so Container Apps can pull it with no credentials.
Write main.bicep declaring: Log Analytics workspace, Container Apps environment, Key Vault, the SYNC JOB (schedule trigger, cron every 4 hours, image from ghcr.io, secret from Key Vault), and the API APP (HTTP ingress, minReplicas 0, maxReplicas 1).
Deploy: az group create → az deployment group create. Then trigger the job once by hand: az containerapp job start.
Watch the execution logs until a cloud run updates your Assets list — laptop closed, running from Azure.
Done today when: The job runs on schedule at zero idle cost, and one Bicep command recreates the whole environment from nothing.
Watch out: Set a $20 budget alert in Cost Management anyway. Expected spend is ~$0, but knowing the guardrail exists — and saying you set it — is itself an AZ-900 talking point.
Day 13 — CI/CD + the metrics endpoint
GitHub Actions workflow on the public repo: push to main → docker build → push to ghcr.io (GITHUB_TOKEN, no secret setup) → az containerapp job update + az containerapp update. Azure auth via OIDC federated credentials — no stored password anywhere. "Passwordless OIDC deploy" is a senior-sounding phrase for a reason.
Prove it: bump the /healthz version string, push, watch Actions go green, curl the live URL, see the new version. Screenshot the green run.
GET /metrics on the API app: reads the Tickets list via Graph (or your trial-tenant copy) and returns JSON — open tickets, due-within-30-minutes count, 7-day SLA compliance %. First curl after idle takes a few seconds — that IS scale-from-zero waking up; in a demo, narrate it as a feature, because it is one.
Log Analytics alert: job execution fails → email you. Make it fire once on purpose; screenshot.
Done today when: git push deploys itself end-to-end, and curl <your-app>/metrics returns live SLA numbers from a container that was asleep a moment earlier.
Day 14 — Package the full stack
README architecture diagram, full picture: users → Power Apps → SharePoint → flows → Teams/email (TCC tenant), PLUS GitHub → Actions → ghcr.io → Container Apps Job/App → Graph → SharePoint (your tenant). One system, two halves, two tenants — and a sentence on why.
Repo docs: Dockerfile, main.bicep, workflow YAML, plus short sections — "why a Job over an always-on container" (periodic workload, scale-to-zero, free grant), "why ghcr over ACR" (public portfolio, zero cost), "how secrets are handled" (Key Vault + OIDC, nothing in code or image), "what I would do next" (managed identity over client secret; Intune device data when licensed).
Extend the demo video ~90 seconds: git push → green pipeline → curl /metrics waking from zero → the Assets list updating from the cloud job.
Harvest evidence before the trial clock matters: every portal screen, job execution history, log streams, the Bicep deployment output.
Send me the repo link — resume bullets same day. Skills line, all honest: Docker, Azure Container Apps (Jobs, scale-to-zero), Bicep IaC, GitHub Actions CI/CD (OIDC), ghcr.io, Key Vault, Log Analytics, Microsoft Graph API, FastAPI, Python.
Done today when: A stranger reading the public repo understands the system, the two-tenant design, and the $0 architecture — and every claim maps to a screenshot, a log line, or a green run.
The honest total
$0. One 30-day clock on the own-tenant half, one card-on-file for the E5 trial (never charged if cancelled — and there is a card-free fallback if that is a dealbreaker). The $100 Azure credit sits untouched as a safety margin. When someone asks what the project cost to run, "zero, by design — and here is the architecture that made it zero" is one more answer that sounds like an engineer.
