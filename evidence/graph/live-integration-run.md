# Live Graph integration evidence

> **HISTORICAL EVIDENCE — captured 2026-08-18.** One run of the opt-in
> integration suite against the real tenant. It has not been re-run, and it is
> not a claim that the tenant is reachable today. For current state, see
> [`../../docs/STATUS.md`](../../docs/STATUS.md).

Executed against the real OpsBridge365 Microsoft 365 tenant with the
`opsbridge-graph` application identity (client credentials), by the repository
author. Tenant, app and site identifiers are deliberately omitted from this
public repo.

## Result, 2026-08-18

```
12 passed, 58 deselected in 10.06s
```

All twelve ran against live Microsoft Graph. The offline suite was unaffected:
`58 passed`. That deselected count is the offline suite as it stood on the day —
it is 106 now, so the same command today prints `12 passed, 106 deselected`.

> **This run is not indefinitely reproducible.** The Microsoft 365 subscription
> is an `O365_BUSINESS_PREMIUM` **trial** with a `nextLifecycleDateTime` of
> **2026-09-16** (measured 2026-08-29). If it lapses, the site and both lists go
> with it and this suite cannot be re-run. The offline suite is unaffected — it
> reaches no tenant.

## What Sites.Selected actually enforces

Measured directly with the application's own token, against a site the app was
never granted (the tenant root) versus the one site it was granted:

| Call | Granted site | Ungranted root site |
|---|---|---|
| site metadata | 200 | **200 - metadata IS readable** |
| `/lists` | 200 (3 lists) | 200 but **0 lists disclosed** |
| `/drive` | 200 | **403 accessDenied** |
| `/drive/root/children` | 200 | **403 accessDenied** |
| `GET /sites?search=*` (enumerate tenant) | — | **403 accessDenied** |

`Sites.Selected` does not hide a site's existence or basic metadata. It withholds
the site's **data** and refuses **tenant-wide enumeration**. Those are the
properties relied on, and they are the ones asserted by the tests.

An earlier version of the test asserted 403 on site *metadata* and failed against
a correctly-configured tenant. The assertion was wrong, not the configuration -
recorded here because the distinction is easy to get backwards.

## Data safety

`test_patch_round_trip_restores_the_original_value` mutates `AssetTag` - chosen
because it is **not** one of the three columns the sync job owns - and restores
the original value in a `finally` block. No test leaves data changed.
