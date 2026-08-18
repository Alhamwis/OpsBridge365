# Live Graph integration evidence

Executed against the real OpsBridge365 Microsoft 365 tenant by the supervising agent.
Tenant, app and site identifiers are deliberately omitted from this public repo.

## Result

```
12 passed, 58 deselected in 10.06s
```

All twelve run against live Microsoft Graph with the `opsbridge-graph` application
identity (client credentials). The offline suite is unaffected: `58 passed`.

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
