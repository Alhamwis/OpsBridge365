# Security policy

## Reporting a vulnerability

Report privately through GitHub's coordinated disclosure flow:

**[Open a private security advisory](https://github.com/Alhamwis/OpsBridge365/security/advisories/new)**

That channel is private to the maintainer until an advisory is published.
Please do not open a public issue for a vulnerability, and please do not
include a working credential in the report — a redacted fingerprint and the
steps to reproduce are enough.

Expect an acknowledgement within 7 days. This is a portfolio project maintained
by one person, not a funded product, so please read that as a realistic
commitment rather than a service level.

## Scope

In scope:

- The application in `app/` — in particular authentication and rate limiting on
  `GET /metrics`, and anything that would let a caller reach Microsoft Graph
  without a token.
- The infrastructure in `infra/` — privilege boundaries, Key Vault access, and
  the separation between the deployment identity and the runtime Graph identity.
- The pipelines in `.github/workflows/` — anything that would let untrusted code
  reach Azure, Microsoft Graph, or the repository's secrets.
- The published container image `ghcr.io/alhamwis/opsbridge365`.

Out of scope:

- The live deployment at `opsbridge-api.purplewave-d90933e8.westus2.azurecontainerapps.io`.
  Please do not run automated scanners, load tests or brute-force attempts
  against it. It is a single scale-to-zero replica on a student subscription;
  testing it costs real money and proves nothing that reading the code does not.
  Report the flaw in the code instead.
- Findings that require a compromised maintainer workstation or a compromised
  GitHub account.
- Missing hardening that is already documented as a known limitation in
  [`docs/SECURITY.md`](../docs/SECURITY.md) — though an argument that a stated
  limitation is worse than described is very much in scope.

## What is already known

`docs/SECURITY.md` records the current posture, including the parts that are
deliberately weaker than a production system would be and why. Reading it first
will save you time.

The one to know about up front: `GET /metrics` is protected by a static bearer
token held in Key Vault, not by short-lived Entra tokens. That trade-off, and
the upgrade path, are documented in `docs/SECURITY.md`.

## Secrets

If you believe a credential has been exposed by this repository, say so in the
report and treat it as live. History rewriting does not rotate a leaked
credential — rotation does.
