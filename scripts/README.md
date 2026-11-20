# Verification scripts

Puzzary separates reproducible checks from checks that intentionally operate on
a running installation.

## Isolated core checks

Run the same groups used by GitHub Actions with the active Python interpreter:

```text
python scripts/run_core_ci.py portable
python scripts/run_core_ci.py security
python scripts/run_core_ci.py plugins
python scripts/run_core_ci.py postgres
```

The runner invokes every script with `sys.executable`, gives each check its own
temporary directory, and adds the repository to `PYTHONPATH`. The `postgres`
group uses `PERMISSIONS_QA_DATABASE` when supplied; otherwise it creates an
isolated SQLite fixture for local use. Browser groups require Playwright's
Chromium package.

Files named `verify_*_live.py` are deliberately excluded. The historical
`verify_print_pdf.py` and `verify_booklet_live.py` checks are also live checks:
they read the configured puzzle database. Live checks read or mutate the
configured installation or expect an existing server at port 8790 and must
only be run against an installation selected by the operator. Deployment
validation is likewise performed by the deployment procedure, not by core CI.

## GitHub Actions

The **Core validation** workflow runs on every pull request and every push to
`main`. It covers supported Python versions, isolated security and plugin
regressions, PostgreSQL concurrency behavior, dependency installation, and a
production Docker image build. Its final `Core validation` job is the required
branch-protection status check.
