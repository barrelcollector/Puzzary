# Security policy

## Supported versions

Until Puzzary reaches a stable release, security fixes are applied to the most
recent public beta and the current `main` branch. Older snapshots are not
supported; upgrade only after taking a validated database and file backup.

| Version | Supported |
| --- | --- |
| Latest `0.1.0-beta.x` | Yes |
| `main` | Development only |
| Older snapshots | No |

## Reporting a vulnerability

Do not open a public issue or discussion. Use GitHub's private vulnerability
reporting form:

https://github.com/barrelcollector/Puzzary/security/advisories/new

Include affected versions, prerequisites, reproduction steps, impact and any
suggested mitigation. Do not include credentials or production user data.

If GitHub does not show the private reporting form while the repository is
still private, wait until public release or contact the maintainer through an
existing private channel. Never fall back to a public issue. Private
vulnerability reporting will be enabled as part of the repository's public
release settings.

The maintainer will acknowledge a report as soon as practical, validate its
scope, coordinate a fix and publication timeline, and credit reporters who want
public recognition. Please allow time for a supported release before publicly
disclosing an unpatched issue.

## Deployment responsibility

Public deployments must replace bootstrap credentials, use HTTPS, protect
session and API credentials, restrict trusted proxies, maintain backups and
apply supported updates. See `docs/docker-deployment.md` and `docs/https-tls.md`.
