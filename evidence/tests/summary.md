# Test evidence — summary

**58 offline + 12 live = 70 tests, all passing.**

```
$ python -m pytest -q
58 passed, 12 deselected

$ python -m pytest -m integration -q      # credentials in the environment
12 passed, 58 deselected in 10.06s
```

---

## The 58 offline tests

No credentials, no network. `httpx` is intercepted by `respx` and MSAL is
replaced by a stub, so no token request and no Graph call leaves the machine. The
suite was re-run with proxies pointed at a dead port to confirm nothing escapes
the mocks.

The integration tests are **deselected by default** —
`addopts = "-ra -m 'not integration'"` in `pyproject.toml` — so a bare
`python -m pytest` on a machine with no credentials is green rather than
skipped-and-noisy.

What they cover that is worth naming:

- **Graph retry and throttling** — 429 and 503 with and without `Retry-After`,
  the 60-second cap on a hostile header, timeouts, transport errors,
  non-retryable 4xx, and malformed JSON.
- **The matching rules** — serial number first, then device name; **a key that
  matches two rows matches nothing**; an unmatched device writes `Unknown`.
- **The SLA computation** — a zero denominator returns `null`, not 0% or 100%; a
  ticket resolved with no due date counts as resolved but is excluded from the
  denominator rather than assumed met.
- **The API's failure surface** — `/healthz` returns 200 with an empty
  environment; `/metrics` returns 503 naming no variable to the caller.

## The 12 live integration tests

Run against the real Microsoft 365 tenant with the `opsbridge-graph` application
identity, client-credentials flow. Captured in
[`../graph/live-integration-run.md`](../graph/live-integration-run.md).

They are opt-in (`pytest -m integration`), they read every value from the
environment, and with any of those unset each test **skips with a reason** rather
than failing.

They are safe to re-run. Exactly one writes — a PATCH round-trip on a single
Assets row, on `AssetTag`, deliberately **not** one of the three columns the sync
job owns — and it restores the original value in a `finally` block.

Several are negative tests, and they are the ones to show a security reviewer:

| Assertion | Result |
| --- | --- |
| A wrong client secret raises `GraphAuthError` | ✅ |
| `/drive` and `/drive/root/children` on an **ungranted** site | **403 accessDenied** |
| `GET /sites?search=*` and `/sites/getAllSites` (tenant-wide enumeration) | **403 accessDenied** |
| Positive control — the **granted** site returns 200 with 3 lists and 4 Assets items | ✅ |

The positive control is not decoration. A 403 proves nothing unless the same call
is shown to succeed where a grant exists — otherwise an empty tenant, a typo in a
site id, or a broken client all look exactly like a working security boundary.

One assertion in that group was **wrong and was corrected**: an earlier version
asserted 403 on an *ungranted site's metadata* and failed against a correctly
configured tenant. `Sites.Selected` does not hide a site's existence; it
withholds the site's data and refuses enumeration. The security property was
real, the assertion was aimed at the wrong surface.

## What the tests do not cover

The suite is the reason to trust the application logic. It is not the reason to
trust the deployment — that is what the cloud evidence in this directory's
siblings is for:

| Question | Where it is answered |
| --- | --- |
| Does the pipeline deploy? | [`../github-actions/pipeline.md`](../github-actions/pipeline.md) — run `32115509179`, four jobs green |
| Does the job run in Azure and write SharePoint? | [`../sharepoint/reconciliation.md`](../sharepoint/reconciliation.md) |
| Does the API serve from a cold start? | [`../azure/deployment.md`](../azure/deployment.md) — 714 ms cold, 143 ms warm |
| Does the failure alert fire? | [`../monitoring/alerting.md`](../monitoring/alerting.md) — the first version did not |

Notably, **no test caught the alert defect.** Nothing in pytest reaches a
scheduled query rule; the only thing that found it was failing the job on purpose
and asking the query what it saw.

## Reproducing

```bash
pip install -e ".[dev]"
python -m pytest -q                        # 58 passed, 12 deselected
python -m pytest -m integration -q         # 12 passed, with tenant credentials set
pytest -m ""                               # both
```
