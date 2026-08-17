# OpsBridge365 Cloud Layer - Azure + Docker (HISTORICAL / SUPERSEDED)

> Superseded by SPEC-cloud-v2-AUTHORITATIVE.md. Kept for intent and history.
> Do NOT restore: ACR, minReplicas=1, APScheduler, always-on container.

OpsBridge 365 — Cloud Layer
Azure + Docker + CI/CD · Days 11–14 of the sprint
Addendum to the 10-Day Sprint Plan. Days 1–10 build the service desk; Days 11–14 build the cloud engineering that a Cloud Computing degree is supposed to prove.
Why this is not bolted on
Your build spec already contains the hook. The Assets list has a LastCheckIn column with the note: "Populated by the Graph sync script in week 5." That roadmap line, engineered properly, IS the cloud layer: a real containerized service, running in Azure, authenticating to Microsoft Graph, writing device data into your SharePoint Assets list on a schedule — with infrastructure as code, a CI/CD pipeline, secrets management, and monitoring.
The result is one coherent system with two halves, and the second half speaks the exact language of cloud job postings:
Docker — multi-stage image build, non-root user, health checks
Azure — Container Apps, Container Registry, Key Vault, Log Analytics, Entra ID app registration
Infrastructure as Code — one Bicep template that recreates the whole environment from nothing
CI/CD — GitHub Actions: push to main → build image → push to registry → deploy, with passwordless OIDC auth to Azure
API engineering — Python + FastAPI + Microsoft Graph, the same Entra ID / identity concepts on your MD-102 and AZ-900 syllabus
And to be precise about the first half: SharePoint, Power Automate, and Power BI are SaaS cloud — that part targets the service-desk jobs you can get hired into now. But you are right that a recruiter scanning for "cloud computing" wants to see Azure infrastructure. After Day 14, your project has both, talking to each other.
Architecture (what the README diagram shows)
git push → GitHub Actions (CI/CD)
  └─ docker build → Azure Container Registry
       └─ deploy → Azure Container Apps
            opsbridge-sync  (FastAPI · Python · Docker)
            ├─ auth:    Entra ID app reg (secret in Key Vault)
            ├─ pulls:   Microsoft Graph — users + devices
            ├─ writes:  SharePoint Assets list (LastCheckIn, ...)
            ├─ exposes: /healthz + /metrics (live SLA stats)
            └─ logs:    Log Analytics

Two features, deliberately: the scheduled Graph→SharePoint sync (background job inside the container), and a small read-only /metrics endpoint that computes live ticket SLA stats from the Tickets list. The second one exists so you can open a terminal in an interview and curl your own API running in Azure. That moment is worth the whole four days.
Day 10.5 — one-evening setup (do this during Days 1–10)
Sign up for Azure for Students (azure.microsoft.com/free/students): $100 credit with a school email, no credit card. If eligibility fails, the regular free account works too.
Install locally: Docker Desktop, Azure CLI (az), Python 3.12, VS Code. Confirm: docker run hello-world.
Confirm you are building the M365 side in YOUR OWN tenant (the Business Basic route from the sprint plan). The Graph app registration needs admin consent — you have that in your own tenant; you will never get it in TCC’s.
Done today when: az login works, Docker runs, and you own a tenant where you are the admin.
Watch out: This is the one item that can block Day 11 for a week if you leave it to Day 11. Do it early, it is 30 minutes.

Day 11 — The service, running locally in Docker
Entra ID app registration in your tenant: client credentials flow, application permissions User.Read.All, Device.Read.All (for the sync source) and Sites.Selected or Sites.ReadWrite.All (to write the Assets list). Grant admin consent.
FastAPI project: /healthz endpoint; a sync module that authenticates with MSAL, pulls users + registered devices from Graph, and PATCHes matching Assets list items via the Graph SharePoint API (match on asset tag in Title; update LastCheckIn, AssignedUser, ComplianceStatus). A device Graph can’t see stays Unknown — that is honest data, keep it.
Background scheduler inside the app (APScheduler): run the sync every 30 minutes; also expose POST /sync to trigger it manually.
Dockerfile: multi-stage (builder + slim runtime), non-root user, HEALTHCHECK hitting /healthz. Run it: docker build, docker run with a local .env, watch the sync update your real Assets list.
Done today when: A container on your laptop updates LastCheckIn on real SharePoint list items, and you screenshotted both sides.
Watch out: Client secret goes in .env, and .env goes in .gitignore BEFORE the first commit. A leaked tenant secret in a public portfolio repo is the one mistake a hiring manager will hold against you.
Day 12 — Into Azure, as code
Write main.bicep declaring: Azure Container Registry (Basic), Log Analytics workspace, Container Apps environment, Key Vault, and the container app itself (secrets referenced from Key Vault, min replicas 1).
Deploy: az group create → az deployment group create. Push your image: az acr build (builds in the cloud — no local push needed).
Move the Graph client secret into Key Vault; the container app reads it as a secret reference. Nothing sensitive in the Bicep file or the image.
Watch the log stream (az containerapp logs show --follow) until a scheduled sync fires from the cloud and your Assets list updates — with your laptop closed.
Done today when: The sync runs from Azure on schedule, and you can tear down and recreate the entire environment from one Bicep command.
Watch out: Container Apps consumption plan + Basic ACR costs roughly $5–7/month against your $100 credit. Set a budget alert at $20 in Cost Management on Day 12 — knowing (and saying) that you did is itself an AZ-900 talking point.
Day 13 — CI/CD + the metrics endpoint
GitHub Actions workflow: on push to main → checkout → docker build → push to ACR → az containerapp update. Authenticate with OIDC federated credentials (azure/login) — no stored Azure password anywhere. That phrase, "passwordless OIDC deploy," is senior-sounding for a reason.
Prove the pipeline: change the /healthz version string, push, watch Actions go green, curl the live URL and see the new version. Screenshot the green run.
Add GET /metrics: reads the Tickets list via Graph and returns JSON — open tickets, tickets due within 30 minutes, SLA compliance % for the last 7 days. Read-only, no auth needed for the demo (or a simple API key header if you want the talking point).
Add a basic alert rule: if the container app’s health probe fails, email you (Log Analytics alert). Screenshot it firing once by stopping the app.
Done today when: A git push deploys itself to Azure with zero manual steps, and curl <your-app>/metrics returns live SLA numbers from your service desk.
Day 14 — Package the full stack
Update the README architecture diagram to the full picture: users → Power Apps → SharePoint → Power Automate → Teams/email, PLUS GitHub → Actions → ACR → Container Apps → Graph → SharePoint. One system, two halves.
Add to the repo: Dockerfile, main.bicep, the workflow YAML, and a docs section — "why Container Apps over App Service/AKS" (right-sized for a single container; AKS would be resume-driven overengineering — say so, it reads as judgment), "how secrets are handled" (Key Vault + OIDC, nothing in code), "what I would do next" (managed identity instead of client secret, Intune device data when licensed).
Extend the demo video by ~90 seconds: git push → green pipeline → curl /metrics → the Assets list updating from the cloud sync.
Send me the repo link — resume bullets same day. The skills line now honestly includes: Docker, Azure Container Apps, Azure Container Registry, Key Vault, Bicep (IaC), GitHub Actions CI/CD, Microsoft Graph API, FastAPI, Python.
Done today when: The repo shows a working service desk AND the cloud infrastructure running part of it — each proven with screenshots, logs, and a green pipeline.
How the finished project reads to a recruiter
"Built a 24/7 IT service desk on Microsoft 365 — SharePoint data layer, four Power Automate flows enforcing a priority matrix and SLA escalation, a Power Apps front end, and a Power BI SLA dashboard. Extended it with a containerized Python microservice (Docker, FastAPI) on Azure Container Apps that syncs directory and device data from Microsoft Graph into the asset inventory and exposes live SLA metrics over a REST API — deployed via Bicep IaC and a GitHub Actions CI/CD pipeline with OIDC, secrets in Key Vault, logs in Log Analytics."
That paragraph contains no exaggeration — every claim maps to a screenshot, a log line, or a green pipeline run you will have produced yourself. That is what makes it a real project: not the buzzwords, the evidence. We will compress it into resume bullets on Day 14.
Sequencing note: Days 11–14 depend on Days 1–2 (the lists) but not on the Power Apps or Power BI days. If you want Azure in your hands sooner, you can run Day 11 in parallel any time after Day 2 — just do the Day 10.5 setup first.
