# Monitoring evidence — and the alert defect that testing found

> **HISTORICAL EVIDENCE — captured 2026-08-18.** A record of one controlled
> failure and what the alert query did about it. The rule has not been re-tested
> since, so this file says the query was correct on that date, not that the alert
> is working today. For current state, see
> [`../../docs/STATUS.md`](../../docs/STATUS.md).

Log Analytics workspace `opsbridge-logs` (PerGB2018, 30-day retention), action
group `opsbridge-alerts`, scheduled query rule `opsbridge-sync-failed`. The
workspace comes from `infra/main.bicep`; the action group and the rule were
created against it afterwards and are not in the template.

| Setting | Value observed 2026-08-18 |
| --- | --- |
| Severity | 2 |
| Evaluation frequency | 5 minutes |
| Lookback window | 15 minutes |
| Action | `opsbridge-alerts` |

---

## 1. The workspace captures the job's output

The `opsbridge-sync` job ran in Azure and Log Analytics captured its summary:

```
users_fetched: 1, devices_fetched: 1, assets_fetched: 4, matched: 1, patched: 1,
unknown_last_check_in: 1
```

That is the run described in
[`../sharepoint/reconciliation.md`](../sharepoint/reconciliation.md) — one device
in the tenant, one confident match, one PATCH written, and one field left alone
because the device had no last-check-in value to report. The container that
produced those lines no longer exists; the log does. That is the point of
shipping a run-and-exit job to a workspace rather than to stdout.

## 2. The alert was tested by breaking the job on purpose

An alert that has never fired is an assumption. So the failure was manufactured:

1. `ASSETS_LIST_ID` was temporarily set to an invalid value.
2. The job was started. Its execution status went to **Failed**.
3. The correct value was restored **immediately**, and the restore was verified.

## 3. The first version of the rule would not have fired

**The rule matched `config_error`. The real failure emitted `graph_error`.**

`app/sync.py` has exactly two failure statuses, and they are not
interchangeable:

| Exit path | JSON status | Exit code | When |
| --- | --- | --- | --- |
| `ConfigError` | `config_error` | 2 | Configuration is missing or unparseable |
| `GraphError` | `graph_error` | 1 | Graph rejected a call — including a valid-looking but wrong list id |

An invalid `ASSETS_LIST_ID` is *present and well-formed configuration that Graph
refuses*, so it fails on the `GraphError` path. The original query looked for the
other one.

Run against the genuinely-failed execution, the original query returned
**0 hits**. The job was Failed, the workspace held the evidence, and the alert
was silent.

## 4. The corrected query, verified against that same failure

The query now matches **both** failure statuses plus `Traceback` and `CRITICAL`
as a catch-all for an unhandled path neither status covers. Against the same
failed execution it returns **2 hits**.

A healthy run afterwards returned the job to **Succeeded**, confirming the
restore was complete and the rule does not fire on success.

| Version | Matches | Hits against the real failure |
| --- | --- | --- |
| First | `config_error` | **0** |
| Corrected | `config_error`, `graph_error`, `Traceback`, `CRITICAL` | **2** |

## 5. What the defect shows

Every other check in this directory confirms something works. This one found a
control that did not.

The defect was invisible to inspection: the rule was syntactically valid, it
pointed at the right workspace, it had a sensible severity and window, and it
matched a real status string the application really does emit. Reading it would
not have caught it. Nothing short of failing the job on purpose and asking the
query what it saw would have.

Two general rules come out of it, and both are cheap:

- **An untested alert is an assumption, not a control.** The gap between "an
  alert exists" and "an alert fires" is exactly the gap between a monitoring
  slide and monitoring.
- **Enumerate every failure path the code can actually take, then match all of
  them.** The catch-all on `Traceback`/`CRITICAL` exists because the enumeration
  was right this time and might not be next time.

The blast radius of the defect, had it shipped: a sync job failing every six
hours, an asset register quietly going stale, and a dashboard reporting no
alerts — which reads as health.

## 6. What this file does not prove, and what has changed since

**Delivery to a human was never proven.** The corrected query was verified to
return 2 hits against the real failure and the action group exists, but no
notification was traced end to end to an inbox. That is a further step and it is
not claimed here.

`opsbridge-alerts` and `opsbridge-sync-failed` were both still present in
`rg-opsbridge365` on 2026-08-29 (`az resource list -g rg-opsbridge365`). Their
existence was re-checked; the query was not re-tested.

A second, independent detection path was added after this capture:
`.github/workflows/health.yml` probes `/healthz` every four hours and on manual
dispatch, with a bounded retry, no secrets, and no redeploy on failure — a failed
run stays red for a human to look at. It watches the API, not the sync job, so it
does not overlap with the rule above. It publishes no uptime percentage: six
probes a day cannot support one.

