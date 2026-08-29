# Evidence — Docker build and local run

> **HISTORICAL EVIDENCE — captured 2026-08-16.** Every transcript below is
> verbatim terminal output from that day and has not been re-run. It is a record
> of one build, not a description of the current image. The Dockerfile and the
> application have both moved on since — the differences are listed under
> [What has changed since this capture](#what-has-changed-since-this-capture).
> For current state, see [`../../docs/STATUS.md`](../../docs/STATUS.md).

**Date:** 2026-08-16 (container clock showed `Mon, 17 Aug 2026 00:46:53 GMT`, UTC)
**Host:** Windows 11 Pro 26200, Docker version 29.1.3 (build f52814d), Git Bash
**Branch:** `main`. (The original note recorded `master`; the repository's branch
is `main` — it is what local and remote HEAD point at, and what
`.github/workflows/deploy.yml` triggers on.)
**Image:** `opsbridge365:local`

Everything below is copied from the terminal. Nothing is reconstructed from memory.

---

## 0. Two code changes made before containerising

### 0.1 `/healthz` must answer without Azure credentials

`app/main.py` called `get_settings().app_version` inside the `/healthz` handler.
With an empty environment that raises `ConfigError`, the app-wide handler turns it
into `503`, and Docker's `HEALTHCHECK` would then kill a container that is merely
unconfigured. A health probe must not depend on secrets. `/healthz` now falls back
to `app.__version__` and logs the reason; `/metrics` — the endpoint that genuinely
needs Graph credentials — still returns `503`.

`tests/test_api.py` changed to match: `test_healthz_is_503_when_configuration_is_missing`
became `test_healthz_is_still_200_when_configuration_is_missing`, and a new
`test_metrics_is_503_when_configuration_is_missing` keeps the 503 path covered.
The "no variable names leaked to callers" assertion is retained on both.

### 0.2 `pyproject.toml` declared a `README.md` that did not exist

`pip install .` therefore failed *before* any container work — confirmed on the host:

```
$ python -m pip download . --no-deps -d /tmp/obtest
  Preparing metadata (pyproject.toml): finished with status 'error'
  error: subprocess-exited-with-error
    File ".../hatchling/metadata/core.py", line 537, in readme
```

A `README.md` was written, and `.dockerignore` excludes `*.md` with an explicit
`!README.md` exception so the builder stage can read it. It is not copied into the
final image.

### 0.3 Test suite after the change

```
$ python -m pytest -q
.........................................................                [100%]
57 passed in 1.69s
```

57 was the whole suite on 2026-08-16, before the integration marker split it. It
is **106 offline plus 12 deselected integration tests** as of 2026-08-29.

---

## 1. `docker build -t opsbridge365:local .`

Tail of the real first (uncached) build:

```
#16 5.830 Building wheels for collected packages: opsbridge365
#16 5.832   Building wheel for opsbridge365 (pyproject.toml): started
#16 5.923   Building wheel for opsbridge365 (pyproject.toml): finished with status 'done'
#16 5.923   Created wheel for opsbridge365: filename=opsbridge365-0.1.0-py3-none-any.whl size=16170 sha256=9a8131f724afa670e07685dc33292118cdf6a2af93cb255a0daed994b2b356b5
#16 5.925 Successfully built opsbridge365
#16 5.956 Installing collected packages: websockets, uvloop, urllib3, typing-extensions, pyyaml, python-dotenv, PyJWT, pycparser, idna, httptools, h11, click, charset_normalizer, certifi, annotated-types, annotated-doc, uvicorn, typing-inspection, requests, pydantic-core, httpcore, cffi, anyio, watchfiles, starlette, pydantic, httpx, cryptography, pydantic-settings, fastapi, msal, opsbridge365
#16 7.585 Successfully installed PyJWT-2.13.0 annotated-doc-0.0.5 annotated-types-0.8.0 anyio-4.14.2 certifi-2026.7.22 cffi-2.1.1 charset_normalizer-3.5.1 click-8.4.2 cryptography-50.0.0 fastapi-0.141.1 h11-0.16.0 httpcore-1.0.9 httptools-0.8.0 httpx-0.28.1 idna-3.18 msal-1.37.0 opsbridge365-0.1.0 pycparser-3.0 pydantic-2.13.4 pydantic-core-2.46.4 pydantic-settings-2.15.0 python-dotenv-1.2.3 pyyaml-6.0.3 requests-2.34.2 starlette-1.6.0 typing-extensions-4.16.0 typing-inspection-0.4.4 urllib3-2.7.0 uvicorn-0.52.3 uvloop-0.22.1 watchfiles-1.2.0 websockets-17.0.1
#16 DONE 7.8s

#17 [runtime 3/5] COPY --from=builder --chown=root:root /opt/venv /opt/venv
#17 DONE 0.4s

#18 [runtime 4/5] WORKDIR /srv
#18 DONE 0.1s

#19 [runtime 5/5] COPY --chown=root:root app ./app
#19 DONE 0.0s

#20 exporting to image
#20 exporting layers
#20 exporting layers 2.0s done
#20 exporting manifest sha256:8425ace7aa2f84eb29dcb3f5021f639bab71e6e342120eea5ac3b1eae0c8d28c 0.0s done
#20 exporting config sha256:cec8e712cba114a372712e659f5d676696cafa3c5c40fd8ba6afb062e2c153cf 0.0s done
#20 exporting attestation manifest sha256:984fe631b305b13d7b7e540fc7f2a0b83c5bcbcfa0b7a397393326df2b4eeac0 0.0s done
#20 exporting manifest list sha256:9d99a678841a87ebe73513e9db8d2680e9decefb7f98944a84ebbf40b500d910 0.0s done
#20 naming to docker.io/library/opsbridge365:local done
#20 unpacking to docker.io/library/opsbridge365:local
#20 unpacking to docker.io/library/opsbridge365:local 0.8s done
#20 DONE 2.9s
```

**Result: build succeeded.** Note `pip install .` installs the runtime dependency set
only — no `pytest`, `respx`, or `ruff` appears in `Successfully installed`, which is the
`[dev]` extra staying out of the image.

Stage layout, from a second (cached) run with `--progress=plain`:

```
#7  [builder 1/7] FROM docker.io/library/python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a
#14 [builder 2/7] RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
#15 [builder 3/7] RUN python -m venv /opt/venv
#13 [builder 4/7] WORKDIR /src
#12 [builder 5/7] COPY pyproject.toml README.md ./
#11 [builder 6/7] COPY app ./app
#16 [builder 7/7] RUN pip install .
#8  [runtime 2/5] RUN groupadd --gid 10001 appuser && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin appuser
#10 [runtime 3/5] COPY --from=builder --chown=root:root /opt/venv /opt/venv
#9  [runtime 4/5] WORKDIR /srv
#17 [runtime 5/5] COPY --chown=root:root app ./app
```

Two things in that listing are no longer how the image is built. The
`python:3.12-slim@sha256:2c941e86...` digest is Docker *resolving* a mutable tag
at build time, not a pin — the Dockerfile said `FROM python:3.12-slim`, so the
same file would have produced a different base a week later. And `RUN pip install .`
resolved dependencies live from the `>=` floors in `pyproject.toml`. Both are
fixed now; see [What has changed since this capture](#what-has-changed-since-this-capture).

### `.dockerignore` effectiveness

The build context transferred is under a kilobyte — the repo's `.git`, `tests/`,
`docs/`, `evidence/`, and caches never reach the daemon:

```
#5 transferring context: 979B done
#6 transferring context: 354B done
```

---

## 2. `docker run --rm opsbridge365:local id -u` — non-root proof

```
$ docker run --rm opsbridge365:local id -u
10001

$ docker run --rm opsbridge365:local id
uid=10001(appuser) gid=10001(appuser) groups=10001(appuser)
```

**`id -u` returned `10001`.** The same holds inside the live server container
(section 3), so it is the server process's uid, not just a one-off `docker run`:

```
$ docker exec opsbridge-test id -u
10001
```

The filesystem is not writable by that user:

```
$ docker run --rm opsbridge365:local sh -c 'touch /srv/probe'
touch: cannot touch '/srv/probe': Permission denied
```

---

## 3. API container and `/healthz`

```
$ docker run -d --rm --name opsbridge-test -p 8099:8000 opsbridge365:local
b6f9f9f1a456e953a0c37d240fb48f7bce9371bbb1ffe3506bcae65dbbc1d6b5
```

No `--env-file`, no `-e` — **the container was started with zero Azure credentials.**

Polling loop (`curl -s -o /tmp/hz.json -w '%{http_code}' http://127.0.0.1:8099/healthz`,
1s apart):

```
attempt 1: HTTP 000
attempt 2: HTTP 200
body: {"status":"ok","version":"0.1.0"}
```

Full response with headers:

```
$ curl -s -i http://127.0.0.1:8099/healthz
HTTP/1.1 200 OK
date: Mon, 17 Aug 2026 00:46:53 GMT
server: uvicorn
content-length: 33
content-type: application/json

{"status":"ok","version":"0.1.0"}
```

`/metrics` — the endpoint that does need configuration — correctly refuses, without
naming a single variable to the caller:

```
$ curl -s -o /tmp/m.json -w 'HTTP %{http_code}\n' http://127.0.0.1:8099/metrics
HTTP 503
{"detail":"Service configuration is incomplete."}
```

That 503 is still the behaviour of an unconfigured container, and the endpoint has
since grown a second reason to return it: with `METRICS_API_TOKEN` unset,
`/metrics` fails **closed** with 503 rather than falling back to serving data. A
configured deployment answers 401 to a caller with no bearer token.

Docker's own `HEALTHCHECK` (stdlib `urllib`, no `curl` in the image) reports healthy:

```
$ docker inspect --format '{{.State.Health.Status}} / log: {{range .State.Health.Log}}exit={{.ExitCode}} out={{.Output}}{{end}}' opsbridge-test
healthy / log: exit=0 out=
```

Container logs — note the deliberate warning on the unconfigured `/healthz`, and the
`503` on `/metrics`:

```
INFO:     Started server process [1]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
Serving /healthz without configuration: Missing or invalid configuration. Set these environment variables: AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, SHAREPOINT_SITE_ID, ASSETS_LIST_ID, TICKETS_LIST_ID. See .env.example for the full list.
INFO:     172.17.0.1:35044 - "GET /healthz HTTP/1.1" 200 OK
Serving /healthz without configuration: ... (repeated for the healthcheck and header probes)
INFO:     127.0.0.1:41242 - "GET /healthz HTTP/1.1" 200 OK
INFO:     172.17.0.1:49160 - "GET /healthz HTTP/1.1" 200 OK
Configuration error: Missing or invalid configuration. Set these environment variables: AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, SHAREPOINT_SITE_ID, ASSETS_LIST_ID, TICKETS_LIST_ID. See .env.example for the full list.
INFO:     172.17.0.1:49162 - "GET /metrics HTTP/1.1" 503 Service Unavailable
```

(The `127.0.0.1:41242` line is the in-container `HEALTHCHECK`; the `172.17.0.1` lines
are the host `curl` calls.)

### Same image, second entrypoint — the sync job

Not a second image. The Container Apps Job just overrides the command:

```
$ docker run --rm opsbridge365:local python -m app.sync
{"status": "config_error", "detail": "Missing or invalid configuration. Set these environment variables: AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, SHAREPOINT_SITE_ID, ASSETS_LIST_ID, TICKETS_LIST_ID. See .env.example for the full list."}
exit code: 2
```

The entrypoint resolves and runs; it exits `2` because no credentials were supplied,
which is the documented `config_error` path. (Variable *names* on stderr in the job's
own log are operator diagnostics, not a response to an internet caller — no values.)

---

## 4. Image size

```
$ docker images opsbridge365:local
IMAGE                ID             DISK USAGE   CONTENT SIZE   EXTRA
opsbridge365:local   9d99a678841a        281MB         66.6MB

$ docker image inspect opsbridge365:local --format '{{.Size}}'
66605912
```

**281 MB on disk uncompressed; 66.6 MB content size (≈66.6 MB / 63.5 MiB to push and
pull).** `python:3.12-slim` accounts for most of it; `build-essential` stays in the
builder stage and ships nothing.

---

## 5. Image contents — what is *not* in there

```
$ docker run --rm opsbridge365:local sh -c 'ls -a /srv'
.
..
app

$ docker run --rm opsbridge365:local sh -c 'ls /srv/app'
__init__.py  config.py  graph.py  main.py  metrics.py  models.py  sharepoint.py  sync.py

$ docker run --rm opsbridge365:local sh -c 'ls /srv/tests; ls /srv/docs'
ls: cannot access '/srv/tests': No such file or directory
ls: cannot access '/srv/docs': No such file or directory

$ docker run --rm opsbridge365:local sh -c 'ls -a /srv/.env /app/.env'
ls: cannot access '/srv/.env': No such file or directory
ls: cannot access '/app/.env': No such file or directory
```

No `.env`, no tests, no docs, no `.git`, no evidence. No `ARG` or `ENV` in the
Dockerfile carries a credential — the only `ENV` values are `PYTHONDONTWRITEBYTECODE`,
`PYTHONUNBUFFERED`, `PATH`, and pip's build-time behaviour flags.

The `/srv/app` listing is the package as it stood on 2026-08-16. It has since
gained `cache.py`, `ratelimit.py`, `security.py` and `demo.py`.

---

## 6. Teardown

```
$ docker stop opsbridge-test
$ docker ps -a --filter name=opsbridge --format '{{.Names}} {{.Status}}'
(no output — the container was started with --rm and is gone)
```

No OpsBridge containers left running or stopped. The `opsbridge365:ctxprobe` tag used
for the build-context measurement was removed with `docker rmi`; `opsbridge365:local`
is the only image left.

---

## What has changed since this capture

Listed so nobody reads the transcripts above as a description of today's image.
None of these has been re-captured to a file; the Dockerfile is the record.

| Then — 2026-08-16 | Now |
| --- | --- |
| `FROM python:3.12-slim` — a mutable tag, resolved to whatever digest Docker had that day | Pinned by digest: `python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217`, with the version kept as a comment |
| `RUN pip install .` — resolved live from the `>=` floors in `pyproject.toml` | Installs from a fully resolved, hash-pinned lock with `pip install --require-hashes --no-deps -r requirements.txt` |
| Suite of 57 tests | 106 offline tests, 12 integration deselected |
| `app/` had eight modules | Plus `cache.py`, `ratelimit.py`, `security.py`, `demo.py` |
| `/metrics` public once configured | Bearer token required; 401 without one, 503 if no token is configured. `/demo/metrics` is the public, synthetic endpoint |

What did **not** change: multi-stage build, non-root uid 10001, no dev tooling in
the runtime layer, and the `HEALTHCHECK` on `/healthz` via stdlib `urllib`.

---

## Checklist — as verified on 2026-08-16

| Requirement | Status |
| --- | --- |
| Multi-stage build, venv at `/opt/venv` | ✅ |
| Runtime deps only, no `[dev]` extra | ✅ (`Successfully installed` has no pytest/ruff/respx) |
| No tests / `.git` / docs / evidence in image | ✅ (§5) |
| Non-root, uid 10001, final `USER` | ✅ `id -u` → `10001` (§2) |
| `PYTHONDONTWRITEBYTECODE` / `PYTHONUNBUFFERED` / `PATH` | ✅ |
| `EXPOSE 8000` | ✅ |
| `HEALTHCHECK` on `/healthz` via stdlib `urllib`, no `curl` | ✅ reports `healthy` (§3) |
| Default `CMD` = uvicorn | ✅ |
| Same image runs `python -m app.sync` | ✅ (§3) |
| `.env` excluded from build context | ✅ (§5, and 979B context) |
| No secrets in `ARG`/`ENV` | ✅ (§5) |
| `/healthz` works with no credentials | ✅ `{"status":"ok","version":"0.1.0"}` |
