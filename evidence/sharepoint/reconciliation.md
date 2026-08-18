# SharePoint reconciliation evidence — the end-to-end proof

This is the headline result: a Container Apps Job running in Azure, reading
Microsoft Graph, and writing a live SharePoint list — with both the confident
path and the honest-gap path proven by the same run.

---

## 1. The first cloud sync had nothing to match

The Microsoft 365 tenant genuinely contained **zero devices**. So the first sync
executed in the cloud found no device to reconcile, and all four Assets rows
stood at `Unknown` rather than at an invented value.

That is the designed behaviour, not a degraded one. The alternative — a
plausible-looking guess in an asset register — is worse than an admitted gap,
because nobody audits a field that looks filled in.

## 2. One synthetic device, then the same job again

One Entra device was created — **`CONTOSO-LT-001`**, with a registered owner —
and the cloud job was re-run. Log Analytics captured the summary:

```
users_fetched: 1, devices_fetched: 1, assets_fetched: 4, matched: 1, patched: 1,
unknown_last_check_in: 1
```

## 3. The result in the live SharePoint Assets list

| Asset | AssignedUser | ComplianceStatus |
| --- | --- | --- |
| `CONTOSO-LT-001` | SAIF EDDINE AL HAMWI | Compliant |
| `CONTOSO-LT-002` | Unknown | Unknown |
| `CONTOSO-DT-003` | Unknown | Unknown |
| `CONTOSO-TB-004` | Unknown | Unknown |

**One confident match written, three honest Unknowns.** Both paths are proven by
one execution, against real data, in the cloud.

The display name is the author's own account in the author's own tenant. The
device names are the fictional seed rows created by
`scripts/provision_sharepoint.py` (`CONTOSO-*`), which is why they are safe to
print.

## 4. Reading the numbers

| Field | Value | What it means |
| --- | --- | --- |
| `users_fetched` | 1 | The tenant has one user — the registered owner |
| `devices_fetched` | 1 | The one synthetic device |
| `assets_fetched` | 4 | All four seeded Assets rows were read |
| `matched` | 1 | Exactly one device resolved to exactly one row |
| `patched` | 1 | One PATCH written to the live list |
| `unknown_last_check_in` | 1 | The matched device had no usable last-check-in value |

That last row is the honesty rule at field granularity. `LastCheckIn` is a **date
column**, and there is no date that means "we do not know" — so the field is
**left untouched** rather than stamped with an invented time, and the count is
surfaced in the summary so the gap is visible instead of silent. `AssignedUser`
and `ComplianceStatus` are text columns, so they can carry the literal string
`Unknown`; the date column cannot, so it is omitted from the PATCH payload
entirely.

The three unmatched rows were not touched, not blanked, and not guessed at. A
device that matches nothing changes nothing.

## 5. What this proves that the offline tests could not

The matching rules — serial number first, then device name; an ambiguous key
matches nothing — were already covered by offline tests against mocks, and the
Graph half was already covered by twelve live integration tests. What none of
those could prove is the whole path end to end:

**Cron-triggered job in Azure → managed identity → Key Vault secret → Microsoft
Graph token → live device and user data → live SharePoint PATCH → observable
result in a list a human can open.**

Every link in that chain is a different failure mode, and this run exercised all
of them at once. The row that changed and the three rows that did not are the
same evidence.
