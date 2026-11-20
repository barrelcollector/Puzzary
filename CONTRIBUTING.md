# Contributing to Puzzary

Thank you for improving Puzzary. Changes should stay focused, preserve saved
puzzles and deployment data, and keep the web interface and supporting API in
sync.

## Development workflow

1. Open or choose a GitHub issue before substantial work.
2. Create a focused branch from `main`.
3. Keep secrets, `.env`, databases, uploads and generated artifacts out of Git.
4. Add or update a focused `scripts/verify_*.py` check.
5. Run the focused check, `scripts/verify_documentation.py`, Python compilation
   and `git diff --check` before opening a pull request.
6. Document any schema, API, plugin-contract or deployment change.

Use disposable SQLite or PostgreSQL databases for tests. Never point a
verification script at a production database unless it is explicitly documented
as read-only and intended for live verification.

## Licensing contributions

Puzzary is licensed under the Apache License 2.0. Unless you explicitly state
otherwise, a contribution intentionally submitted for inclusion is provided
under Apache-2.0 as described by section 5 of that license. By submitting a
contribution, you confirm that you have the right to provide it under those
terms. Identify copied or adapted third-party material and preserve its license
and attribution.

No contributor license agreement is required at this time.

## AI-assisted contributions

AI tools may assist development, but an AI system is not a contributor or
copyright holder. The human submitter remains responsible for reviewing,
testing and understanding the change; checking that it does not reproduce
incompatible third-party material; and confirming that they have authority to
license the contribution under Apache-2.0. Disclose substantial AI assistance
in the pull request when it materially shaped the implementation.

## Security and conduct

Do not report vulnerabilities in a public issue. Follow [SECURITY.md](SECURITY.md).
All project interactions must follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
