# Progressive Web App

Puzzary can be installed from supported desktop and mobile browsers. The
installed application uses the same server, accounts, permissions, and URLs as
the regular web portal; installing it does not create a separate account or
copy the database.

## Requirements

Service workers and installation require a secure browser context. Use HTTPS
for a remote deployment. Browsers treat `http://localhost` as secure for local
development, but a plain HTTP LAN address is not installable. See
[`https-tls.md`](https-tls.md) for native TLS and trusted reverse-proxy examples.
For a server that stays private on a home network, follow the
[LAN-only offline travel guide](lan-only-offline.md). It explains local DNS,
trusted certificates on phones and tablets, and why public exposure is not
required.

The manifest and service worker are served at `/static/site.webmanifest` and
`/service-worker.js`. A reverse proxy must preserve those paths and must not
rewrite the service-worker scope. Puzzary sends `Service-Worker-Allowed: /`.

## Caching and privacy

The service worker caches only the public application shell, core static files,
and installed plugin static assets. Authenticated HTML, API responses, puzzle
payloads, credentials, and solutions are network-only. If the server cannot be
reached, navigation displays a small offline status page rather than a cached
account or puzzle page.

Logout and account changes clear all reserved user-data caches. Full opt-in
offline puzzle downloads and progress synchronization are a separate feature;
the shell cache does not imply that a puzzle is available offline.

## Updates

The worker uses a versioned shell cache and removes superseded shell caches when
activated. When a new worker is ready, the web portal displays a refresh prompt
so an active puzzle is not replaced without the player's choice. Plugin static
assets use network-first loading so module updates do not remain hidden behind
an older cached file.

Puzzary remains fully usable as a normal browser application when installation
is unavailable or declined. Push notifications share the same service worker
and remain explicit opt-in functionality.
