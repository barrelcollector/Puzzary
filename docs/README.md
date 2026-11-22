# Puzzary documentation

Puzzary is web first: ordinary player and administrator workflows are available
in the application. These guides explain installation, operation, advanced
features, automation, and extension development.

## Install and operate

- [Docker deployment](docker-deployment.md): first start, persistent data,
  upgrades, backups, restores, and troubleshooting.
- [HTTPS and trusted reverse proxies](https-tls.md): public URLs, secure cookies,
  forwarding rules, native TLS, and verification.
- [Security policy](../SECURITY.md): supported versions and private reporting.
- [Release notes](releases/v0.1.0-beta.1.md): current beta scope and upgrade
  warnings.

## Use and administer

- [Using Puzzary](using-puzzary.md): library, play, progress, offline downloads,
  feeds, notifications, printing, and Group Solve.
- [Administration](administration.md): accounts, permissions, publishing,
  content, themes, modules, and optional sign-in providers.
- [Group Solve](group-solve.md): hosting, joining, guests, privacy, and realtime
  collaboration behavior.
- [Publishing and printing](publishing-and-printing.md): puzzle sheets, PNG/PDF
  output, issues, and immutable published snapshots.
- [Appearance themes](appearance-themes.md)
- [Browser notifications](browser-notifications.md)
- [Private RSS and Atom feeds](rss-atom-feeds.md)
- [Word-bank import and export](word-bank-imports.md)

## Installable web app and offline play

- [Progressive Web App](progressive-web-app.md): browser installation, caching,
  privacy, and updates.
- [Offline puzzle play](offline-puzzle-play.md): eligibility, local checking,
  synchronization, conflicts, and the module contract.

## APIs and extensions

- [API clients and print automation](api-clients.md): authentication,
  permissions, generation, print manifests, and issue endpoints.
- [Puzzle module contract](puzzle-modules.md): package format, installation,
  settings, lifecycle, and contributor API.
- The running server's `/docs` page is the authoritative OpenAPI reference for
  its installed core version and modules.
- Official puzzle and content packages are maintained in the
  [Puzzary Official Plugins repository](https://github.com/barrelcollector/puzzary-official-plugins).

## Contribute and verify

- [Contributing](../CONTRIBUTING.md)
- [Verification scripts](../scripts/README.md)
- [Changelog](../CHANGELOG.md)
- [Code of conduct](../CODE_OF_CONDUCT.md)
