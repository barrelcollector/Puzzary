# Local Docker deployment

This guide runs Puzzary with Docker Compose on a single host. Examples use
generic local names and placeholders; replace them for your environment and do
not commit `.env`, database dumps, or credentials.

## Prerequisites

- Docker Engine
- Docker Compose v2 (`docker compose version`)
- Git, or a downloaded source archive

## First start

Copy the environment template and replace every `change-*` value. The password
inside `DATABASE_URL` must match `POSTGRES_PASSWORD`.

```bash
cp .env.example .env
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl --fail http://localhost:8790/health
docker compose exec app cat /data/bootstrap-admin-setup.txt
```

Open the one-time setup link, choose the initial administrator password, and
then sign in at `http://localhost:8790`. The setup token expires after 24 hours,
is stored in PostgreSQL only as a hash, and cannot be reused. Its delivery file
is mode `0600` where the host filesystem supports Unix permissions and is
deleted after successful setup. Container logs contain only a pointer to the
file, never the setup link itself. Do not copy the file into tickets or chat.
If the link expires before use, restart only the app container and read the
replacement file:

```bash
docker compose restart app
docker compose exec app cat /data/bootstrap-admin-setup.txt
```

Set `PUZZARY_BOOTSTRAP_ADMIN_NAME` in `.env` before the first start if the
initial account should use a name other than `admin`. Existing databases keep
their users and password hashes; the bootstrap file is only created when no
usable administrator password exists.

Set `APP_BASE_URL` to the externally reachable HTTPS origin before printing QR
codes or using Puzzary behind a reverse proxy.

HTTP remains the compatibility default. Public deployments should follow the [HTTPS and trusted reverse-proxy guide](https-tls.md), including its secure-cookie, proxy allowlist, WebSocket, certificate-mount, and renewal checks.

## Persistent data

The supplied Compose file bind-mounts `./data` into the application at `/data`:

- `./data/postgres` contains the PostgreSQL cluster;
- `./data/uploads` contains uploaded work and photos;
- other application-owned files under `/data` survive container replacement.

Completed-work uploads are limited to 10 MB and to JPEG, PNG or WebP content. They are private application data, not static web files. Include `./data/uploads` in backups that need to preserve photo submissions; permanently deleting a submission or puzzle removes its corresponding stored photo files.

`docker compose down` removes containers and the private network but leaves
these bind-mounted files intact. Do not use `docker compose down --volumes` as
a data-management strategy, and do not copy a live PostgreSQL data directory as
your only database backup.

## Schema migrations and upgrades

Puzzary creates missing tables and applies its built-in, idempotent schema
migrations during application startup. There is no separate Alembic command.
Back up the database before running a newer revision; do not run an older image
against a database that has already been migrated by newer code.

```bash
docker compose stop app
docker compose build --pull app
docker compose up -d
docker compose ps
curl --fail http://localhost:8790/health
docker compose logs --tail=100 app
```

Stopping only `app` keeps PostgreSQL available while the new image is built.
After the new core version is healthy, sign in to **Admin -> Plugins**, choose
**Update All Modules**, and restart only `app` once more. This installs the
checksummed package versions paired with the new core catalog. Until that
restart, an incompatible older package is shown as unavailable; its puzzles and
progress remain stored and are not replaced by a bundled fallback. Verify every
installed puzzle type after the restart before removing the rollback image.

If startup or migration fails, keep the failed logs, restore the matching
pre-upgrade database backup, and return to the previous application revision.

### Rollback procedure

Keep the previous image or source revision until the upgrade has passed health,
login, puzzle, upload, module, and print checks. If the new application cannot
start or a migration check fails:

1. Stop only the application container and preserve its logs.
2. Restore the validated pre-upgrade database dump and matching application-data
   backup into an empty, verified target.
3. Rebuild or retag the exact previous application revision.
4. Start the application and repeat the health and login checks.

Never point an older image at a database already modified by newer code. A
rollback is the previous image **plus** its matching database and file backup.

## Database backup

Choose a backup directory outside `./data/postgres`. Substitute the database
name and user from your `.env` file without putting a password on the command
line.

```bash
mkdir -p backups
DB_USER="<database-user>"
DB_NAME="<database-name>"
docker compose exec -T db pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > backups/puzzary.dump
docker compose exec -T db pg_restore --list < backups/puzzary.dump > /dev/null
test -s backups/puzzary.dump
```

The `pg_restore --list` step validates that PostgreSQL can read the custom-format
archive. Move completed backups to storage outside the Docker host and apply an
appropriate retention policy.

Back up uploads separately from the database:

```bash
tar --exclude='./data/postgres' -czf backups/puzzary-files.tar.gz ./data
```

## Restore drill

Test restores on a disposable installation before relying on them. Restoring
with `--clean` replaces database objects, so stop the application and verify the
target database and backup path first.

```bash
DB_USER="<database-user>"
DB_NAME="<database-name>"
BACKUP_FILE="backups/puzzary.dump"
docker compose stop app
docker compose exec -T db pg_restore --clean --if-exists --no-owner --no-privileges -U "$DB_USER" -d "$DB_NAME" < "$BACKUP_FILE"
docker compose start app
curl --fail http://localhost:8790/health
```

Restore `puzzary-files.tar.gz` only into an empty, verified application data
directory. Never overwrite an unrelated path or a running PostgreSQL data
directory.

## Troubleshooting

```bash
docker compose ps
docker compose logs --tail=200 app db
docker compose config
```

- A database authentication failure usually means `POSTGRES_PASSWORD` and the
  password embedded in `DATABASE_URL` differ.
- An unhealthy public URL does not necessarily mean the local service failed;
  check `http://localhost:8790/health` before troubleshooting a proxy.
- Files owned by a different host user may prevent PostgreSQL or uploads from
  starting. Preserve ownership and permissions when moving `./data`.

For puzzle and game extensions, see
[Puzzle module contributor guide](puzzle-modules.md).
