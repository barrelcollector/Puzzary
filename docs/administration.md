# Administering Puzzary

The web interface under **Admin** is the primary management surface. The API
supports automation but does not replace a required web workflow.

## Accounts, roles, and sign-in

Puzzary does not enable public self-registration by default. Administrators
create and deactivate accounts, issue one-time password setup links, revoke
sessions, and rotate API tokens under **Admin > Users**. Deployments that want
verified public signup can enable and configure [public account
registration](public-registration.md) under **Admin > Settings**. Do not remove
the last active account with the permissions needed to administer users, roles,
and server settings.

Roles provide reusable permission sets. Per-account allows and denies refine
those sets, with an explicit deny taking precedence. Review the effective
permission preview before saving elevated access.

Built-in accounts are the default. Deployments with an existing identity
provider can optionally configure [OpenID Connect](oidc-sso.md) while retaining
a local administrator recovery path.

## Puzzle modules

Puzzle implementations and optional content packages are installed under
**Admin > Plugins**. Use **Update All Modules** to install or update the official
catalog, or install a reviewed GitHub release or ZIP with its published SHA-256
digest. Each puzzle module can be enabled, disabled, or updated independently.

Module code runs with the same process access as Puzzary. Install only trusted
packages. A restart is required after installing, updating, uninstalling, or
restoring executable module code. The
[official module repository](https://github.com/barrelcollector/puzzary-official-plugins)
contains the maintained puzzle catalog; the [module contract](puzzle-modules.md)
documents third-party packaging and lifecycle rules.

## Library content

Administrators can create and organize puzzles, categories, tags, word banks,
and vocabulary records. Word-bank and dictionary management are shared core
services used only by modules that declare those source requirements; puzzle
implementations and packaged vocabulary data are not bundled into core.

Use the web import preview before committing external word-bank data. It shows
mapping, duplicate handling, warnings, attribution, and rejected rows. See the
[word-bank transfer guide](word-bank-imports.md) for supported formats and
network security limits.

## Publishing and print output

Publishing tools assemble printable puzzles and text sections into ordered
issues. Publishing freezes the print snapshots used by that issue so later
puzzle or module changes do not silently alter the published edition. See
[publishing and printing](publishing-and-printing.md).

## Appearance, feeds, and notifications

- Manage the server default and portable custom themes under **Admin > Themes**.
  See [appearance themes](appearance-themes.md).
- Users create their own revocable private feeds; see
  [RSS and Atom feeds](rss-atom-feeds.md).
- Browser notification delivery requires HTTPS outside localhost; see
  [browser notifications](browser-notifications.md).

## API access

API tokens are shown once and stored only as hashes. Give automation accounts
only the permissions they require, and separate read-only printing from content
generation when practical. The running server publishes its exact OpenAPI
schema at `/docs`; common automation patterns are covered by the
[API client guide](api-clients.md).
