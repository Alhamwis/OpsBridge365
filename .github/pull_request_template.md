## What changed, and why

<!-- One paragraph. What does this change, and what problem does it solve?
     If it fixes a defect, say what the defect actually did. -->

## Evidence

<!-- The repository's standard is that a claim cites a command or an artifact.
     Paste the output, or link the Actions run. "Tested locally" is not evidence. -->

- [ ] `python -m pytest -q` — result:
- [ ] `ruff check .` — result:
- [ ] For infrastructure changes: `az bicep build` and `az deployment group what-if` — result:

## Risk

- [ ] This changes who can reach `/metrics`, or what it returns
- [ ] This changes a privilege, role assignment, or identity
- [ ] This changes what the deployment pipeline can access
- [ ] This changes a dependency, base image, or action pin
- [ ] None of the above — documentation, tests, or internal refactor

## Checks

- [ ] No credential, token, tenant id, subscription id, or personal identifier
      appears in the diff — including in test fixtures and example files
- [ ] Documentation updated where this changes documented behaviour
- [ ] Any claim about the live system is dated, or labelled as historical
- [ ] New behaviour has a test that fails without the change
