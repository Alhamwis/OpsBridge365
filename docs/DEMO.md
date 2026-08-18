# Demo script — 5 minutes

A run sheet for walking an interviewer through OpsBridge365.

> **Which demo you can give depends on what exists.** Nothing is deployed to Azure
> today, so **Track B — the local Docker demo — is the one that runs right now**, and
> it is a complete, honest demo on its own. Track A is written for the day the
> accounts exist. Do not describe Track A as if it has happened.

---

## Before you start

```bash
git pull
python -m pytest -q                       # confirm green
docker build -t opsbridge365:local .      # pre-build; never build live
docker image inspect opsbridge365:local --format '{{.Size}}'
```

Have open, in this order: the repo, a terminal, and `README.md` scrolled to the
architecture diagram. Close everything else. Pre-build the image — a live
`docker build` is three minutes of watching pip.

**The one thing to say in the first fifteen seconds**, because it sets the frame
for everything after it:

> "This is the cloud half of an IT service desk tool — a scheduled job that
> reconciles device data from Microsoft Graph into a SharePoint asset list, and an
> API that reports live SLA numbers. It's fully built and tested. It is *not*
> deployed, because the Azure and Microsoft 365 accounts it needs don't exist yet —
> that's a signup step, not an engineering one. Everything I show you runs on this
> laptop."

Saying that first turns the gap into a stated boundary instead of something they
discover and you explain away.

---

## Track B — the local demo (runs today)

### 0:00 — The problem (30 seconds)

No screen yet. Just say it:

> "A service desk has two chores nobody does. The asset register drifts from what
> the directory actually knows — who has which laptop, is it compliant, when did it
> last check in. And 'how many tickets are about to breach SLA' gets answered by
> opening a list and counting. This automates both."

### 0:30 — The architecture (60 seconds)

`README.md`, the Mermaid diagram. Trace one path with your finger:

> "Push to main. Actions builds one image and pushes it to GitHub's container
> registry. Bicep deploys it twice — as a Container Apps *Job* on a cron, and as a
> Container *App* with HTTP ingress. Both pull the same image; the job just
> overrides the command. Both read one secret from Key Vault using a managed
> identity. Both log to Log Analytics."

Then the two decisions worth volunteering unprompted:

> "Two things I'd point at. The sync is a Job, not an always-on container with a
> scheduler inside — the workload is periodic, so it runs and exits and bills
> nothing in between. And the API scales to zero. Together they're why an idle
> month costs zero rather than nearly zero."

### 1:30 — The tests (45 seconds)

```bash
python -m pytest -q
```

```
57 passed in 1.71s
```

> "Fifty-seven tests, all offline — httpx is intercepted and MSAL is stubbed, so no
> test ever needs a credential or touches the network. That includes the retry
> path: 429 and 503 with `Retry-After` honoured, timeouts, transport errors, and
> malformed JSON."

Then open `app/metrics.py` and point at one line:

> "This is the bit I'd defend hardest. If nothing was resolved in the last seven
> days, SLA compliance returns `null` — not 0%, not 100%. And a ticket resolved
> without a due date is counted as resolved but excluded from the denominator,
> never assumed met. A metric that lies confidently is worse than no metric."

### 2:15 — The container (90 seconds)

```bash
docker run -d --rm --name demo -p 8000:8000 opsbridge365:local
curl -s http://localhost:8000/healthz
```

```json
{"status":"ok","version":"0.1.0"}
```

> "No `--env-file`, no `-e`. Zero credentials. `/healthz` still answers 200 — on
> purpose. A health probe that needs secrets means the orchestrator kills a
> container that's merely unconfigured."

```bash
curl -s -w '\nHTTP %{http_code}\n' http://localhost:8000/metrics
```

```
{"detail":"Service configuration is incomplete."}
HTTP 503
```

> "The endpoint that *does* need credentials refuses — and notice it doesn't name a
> single variable to the caller. The server log names all six for the operator."

```bash
docker logs demo | tail -3
docker run --rm opsbridge365:local id -u        # -> 10001
docker exec demo id -u                          # -> 10001, the live server process
```

> "Non-root, uid 10001, and the filesystem isn't writable by that user. Multi-stage
> build, so the compiler toolchain stays in the builder — and the dev extra is never
> installed, so there's no pytest or ruff in the runtime image."

Same image, second entrypoint:

```bash
docker run --rm opsbridge365:local python -m app.sync; echo "exit: $?"
```

```
{"status": "config_error", "detail": "Missing or invalid configuration. ..."}
exit: 2
```

> "Same image, different command — that's exactly what the Container Apps Job does.
> It exits 2 because there are no credentials, which is the documented config-error
> path. Structured JSON on stdout and a meaningful exit code, because a cron job
> that fails silently is worse than one that doesn't run."

```bash
docker stop demo
```

### 3:45 — The infrastructure (45 seconds)

```bash
bicep build infra/main.bicep --stdout > /dev/null && echo "compiles clean"
```

Open `infra/main.bicep`, scroll to `minReplicas: 0` and to the Key Vault secret
block:

> "One file, one resource group. The secret is a `@secure()` parameter, so ARM
> never logs it. It goes into Key Vault, and the containers get a Key Vault
> *reference* resolved by a managed identity at startup — the plaintext is never on
> the app resource, and nothing is emitted as a deployment output. The identity's
> only permission anywhere in Azure is 'read secret values from this one vault'."

Then `deploy.yml`, at the `azure/login` step:

> "And there's no Azure password anywhere. GitHub mints an OIDC token scoped to
> this repo, Azure exchanges it, the token dies with the job. There are two app
> registrations on purpose — the one that can deploy has no secret, and the one
> with a secret has no Azure permissions at all."

### 4:30 — Where it stands (30 seconds)

`README.md`, the status table. Do not skip this; it is the most credible part.

> "Here's what's real. Green is built and tested locally — the service, the tests,
> the container. Amber is written but never run against a cloud, because there's no
> subscription: the Bicep compiles but has never been deployed, and the workflows
> have never executed because this repo doesn't have a GitHub remote yet. Red is
> blocked on account signups — a student subscription, my own Entra tenant, an E5
> trial for SharePoint. Those are forms, not engineering. When they exist, the
> deploy is a `git push`."

Then, if there is time:

```powershell
powershell -NoProfile -File scripts/verify-opsbridge.ps1
```

> "This is a verification script that never lies to me. Anything it couldn't check
> is SKIP, never PASS — so right now it passes the local checks and skips every
> cloud one, with the reason. An unauthenticated run tells you exactly what it
> didn't verify."

---

## Track A — the live cloud demo (once deployed)

**Only usable after the accounts exist and a deploy has succeeded.** Same opening,
then:

1. **Show the green Actions run** — test → build → deploy → verify, and the job
   summary showing `/healthz` returned 200 and the job's trigger type is
   `Schedule`.
2. **Wake the API from zero.** `curl https://<fqdn>/metrics`. The first call takes
   a few seconds. Narrate it: *"that pause is the container starting — there was no
   replica running a second ago, which is why an idle month costs nothing."* Then
   curl again to show it is instant while warm.
3. **Run the sync on demand.** `az containerapp job start -g rg-opsbridge365 -n
   opsbridge-sync`, then `az containerapp job execution list -o table`.
4. **Show the SharePoint Assets list updating** — side by side with the job's JSON
   summary, pointing at the rows that stayed `Unknown` and explaining why that is
   the honest outcome.
5. **Show the Log Analytics stream** — the sync's log lines from a container that
   no longer exists.
6. **Push a trivial change** (bump `__version__`), watch the pipeline go green, and
   curl `/healthz` to see the new version.

Keep Track B's status-table close either way; even with a live deploy, being able
to say what is *not* proven is the differentiator.

---

## Fallbacks

| If this fails | Do this |
| --- | --- |
| **No network at all** | Track B needs none, once the image is built. The whole demo is `docker run` and `curl localhost` |
| **Docker daemon is down** | Fall back to `pytest -q`, then walk the code: `metrics.py` for the SLA logic, `sync.py` for the ambiguity rule, `main.py` for the error handling. Show `evidence/docker/build-and-run.md` — captured terminal output of the container demo you cannot run live |
| **The image isn't built and there's no time** | `evidence/docker/build-and-run.md` has every command and its real output: `id -u` returning 10001, the 200 on `/healthz`, the 503 on `/metrics`, the healthcheck reporting healthy, the image contents |
| **Live cloud demo breaks mid-interview** | Say so, and switch: *"that's the cold start not co-operating — here's the same thing locally."* Then Track B. A recovered demo beats a flawless one for showing how you handle production |
| **They only have 2 minutes** | Tests (0:30) → `/healthz` with no credentials (0:45) → `minReplicas: 0` and the OIDC step (0:30) → status table (0:15) |

---

## Questions to expect right after, and where the answers are

- *"Why Container Apps and not Functions or AKS?"* →
  [`INTERVIEW-NOTES.md`](INTERVIEW-NOTES.md)
- *"How is the SLA number actually computed?"* → same, and `app/metrics.py`
- *"What happens when Graph throttles you?"* → same, and `app/graph.py`
- *"What would you do differently at 10× scale?"* → same
- *"Why isn't it deployed?"* → the status table, and say it plainly: the accounts
  don't exist yet, and every step that needs one is listed in
  [`DEPLOYMENT.md`](DEPLOYMENT.md) Phase 1.

**Never claim a cloud run happened.** If asked whether it has ever run in Azure,
the answer is "no, not yet" — followed by exactly what *has* been verified and how.
That answer is worth more than the demo.
