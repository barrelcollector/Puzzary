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
```

Open `http://localhost:8790`, sign in with the documented bootstrap account,
and change its password immediately. Set `APP_BASE_URL` to the externally
reachable HTTPS origin before printing QR codes or using Puzzary behind a
reverse proxy.

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
If startup or migration fails, keep the failed logs, restore the matching
pre-upgrade database backup, and return to the previous application revision.

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
