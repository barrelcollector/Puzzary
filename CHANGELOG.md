# Changelog

Puzzary follows [Semantic Versioning](https://semver.org/). This file records
user-visible changes to the core application. Puzzle modules and integrations
are versioned independently in their own repositories.

## [Unreleased]

### Changed

- Puzzle implementations, templates, browser assets, module-specific guides,
  and packaged content now have one canonical home in the official package
  repository. Core loads installed packages and reports missing modules without
  silently substituting a core implementation.

## [0.1.0-beta.1] - 2026-11-20

### Added

- First-run administrator setup without a universal default password.
- An independently installable catalog of official puzzle types.
- Web and API workflows for puzzle generation, play, progress, groups,
  publishing, printable PDF/PNG output, uploads, themes, feeds, notifications,
  permissions, OIDC sign-in, and module management.
- Portable core CI, dependency auditing, Python security scanning, and a
  full-history secret scan.
- Apache-2.0 licensing, contribution guidance, security policy, and public-beta
  deployment documentation.

### Release status

- Public beta. Back up the PostgreSQL database and application data before
  upgrading.

[Unreleased]: https://github.com/barrelcollector/Puzzary/compare/v0.1.0-beta.1...HEAD
[0.1.0-beta.1]: https://github.com/barrelcollector/Puzzary/releases/tag/v0.1.0-beta.1
