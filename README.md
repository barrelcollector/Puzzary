# Puzzary

**A self-hosted home for puzzles, games, and the people who solve them.**

Puzzary is a self-hosted, extensible puzzle and game library with a flexible API.
It brings a searchable library, collaborative play, publishing, printing,
and administration into one web-first application. Run it on your own server,
keep control of your data, and add puzzle types through modular plugins that are
released independently from core.

## Why Puzzary?

- **One library for digital and printable play.** Organize, publish, solve, and
  print from the same source.
- **Built for groups.** Host live Group Solve sessions with accounts or
  carefully scoped guests.
- **Web first.** Players and administrators can use the full product from the
  browser; the API supports the same workflows for automation and integrations.
- **Modular by design.** Puzzle implementations install and update separately
  from core, so new game types do not require a new Puzzary release.
- **Self-hosted and private.** PostgreSQL stores your library, accounts,
  progress, permissions, and publishing history on infrastructure you control.
- **Ready beyond the browser.** Install the Progressive Web App, save eligible
  puzzles for offline play, create private feeds and notifications, and produce
  PDF or PNG output.

## Puzzle modules

Puzzary core does not bundle canonical puzzle implementations. Administrators
install the official catalog or a reviewed third-party module from **Admin >
Plugins**. Existing puzzle records remain stored if a module is unavailable.

The official puzzle types and content packages live in the
[Puzzary Official Plugins repository](https://github.com/barrelcollector/puzzary-official-plugins).
Module authors can use the [puzzle module contract](docs/puzzle-modules.md) to
build and distribute additional types.

## Quick start

You need Docker Engine and Docker Compose v2. For production upgrades, backups,
restores, and reverse-proxy guidance, use the full
[Docker deployment guide](docs/docker-deployment.md).

```bash
git clone https://github.com/barrelcollector/Puzzary.git
cd Puzzary
cp .env.example .env
```

Replace every `change-*` value in `.env`, then start Puzzary:

```bash
docker compose up -d --build
docker compose ps
curl --fail http://localhost:8790/health
```

Open `http://localhost:8790`. A fresh installation creates a one-time initial
administrator setup link. Retrieve it without exposing it in logs or chat:

```bash
docker compose exec app cat /data/bootstrap-admin-setup.txt
```

After signing in, open **Admin > Plugins** and choose **Update All Modules**, or
install only the modules you want. Restart the application container when the
page reports that a restart is required.

Public deployments should set `APP_BASE_URL`, use HTTPS, and follow the
[trusted proxy and TLS guide](docs/https-tls.md).

## Using Puzzary

Players browse the library, solve supported puzzles in the browser, save
progress, print puzzle sheets, join Group Solve sessions, and manage their own
offline downloads, feeds, and notifications from the web interface.

Administrators manage accounts, roles, puzzle availability, publishing,
themes, content sources, and installed modules from the web interface. Built-in
accounts are the default; OpenID Connect is optional for deployments that
already use an identity provider.

Start with the [player guide](docs/using-puzzary.md) or the
[administrator guide](docs/administration.md).

## Documentation

The [documentation index](docs/README.md) organizes the complete guides. Common
starting points include:

- [Installation, upgrades, backup, and restore](docs/docker-deployment.md)
- [Progressive Web App installation](docs/progressive-web-app.md)
- [Offline puzzle play](docs/offline-puzzle-play.md)
- [Group Solve](docs/group-solve.md)
- [Publishing and printing](docs/publishing-and-printing.md)
- [API clients and print automation](docs/api-clients.md)
- [Puzzle module installation and development](docs/puzzle-modules.md)
- [Development and verification](CONTRIBUTING.md)

The running server exposes its current OpenAPI documentation at `/docs`.

## Project status

Puzzary is public beta software. Review the [changelog](CHANGELOG.md), the
[current beta release notes](docs/releases/v0.1.0-beta.1.md), and the
[security policy](SECURITY.md) before operating a public installation. Back up
the PostgreSQL database and application data before every upgrade.

## AI-assisted development disclosure

Puzzary has been developed substantially through AI-assisted, sometimes called
“vibe coding,” workflows. A human maintainer directs the product, reviews and
accepts changes, runs the project’s tests, and is responsible for releases and
licensing decisions. AI-produced code can contain mistakes, so users should
treat the public-beta status, test coverage, security policy, and backup advice
as important safeguards rather than guarantees. Contributions that use AI must
follow the [AI-assisted contribution requirements](CONTRIBUTING.md#ai-assisted-contributions).

## License and contributions

Puzzary core is licensed under the [Apache License 2.0](LICENSE). Puzzle modules
and optional content packages are independently versioned and may carry their
own notices. See [NOTICE](NOTICE) and the license files supplied with each
installed package.

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) and the
[community code of conduct](CODE_OF_CONDUCT.md) before opening a pull request.
