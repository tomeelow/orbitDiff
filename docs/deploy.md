# Deploying Orbit Diff

One machine running `deploy/compose.yml`, behind Caddy. The stack, a full backfill, tiles,
backups and a restore were run on a laptop with a local certificate and a local mail catcher.
The release workflow and `deploy/update.sh` haven't run yet, because they need a registry and
a machine, and nothing here has touched a real account.

## What you need to provide

| What | Used for | Where it goes |
|---|---|---|
| A Linux **x86-64 machine** with Docker, SSH access, about 40 GB of disk | Runs everything. The PostGIS image is amd64 only | n/a |
| A **domain** with its A record pointing at the machine, ports 80 and 443 open | Caddy gets the certificate by itself | `ORBITDIFF_DOMAIN` |
| An **email provider**'s SMTP host, username and password, and a verified sender | Sign-in links and alerts | `ORBITDIFF_SMTP_*`, `ORBITDIFF_MAIL_FROM` |
| An **S3-compatible bucket** and an access key (optional, strongly advised) | Backups that survive losing the machine | `RCLONE_CONFIG_BACKUP_*`, `ORBITDIFF_BACKUP_BUCKET` |
| A **GitHub token** with `read:packages` on the machine, unless you make the packages public | Pulling images from GHCR | `docker login ghcr.io` |

Two more values you generate yourself: `POSTGRES_PASSWORD` and `ORBITDIFF_SIGNING_KEY`, each
from `openssl rand -hex 32`.

## First deploy

**1. Publish images.** Push a version tag. `.github/workflows/release.yml` builds the four
images and pushes them to `ghcr.io/<owner>/`. `main` never deploys itself.

```bash
git tag v1.0.0 && git push origin v1.0.0
```

**2. On the machine,** check out that tag and fill in the settings:

```bash
git clone <your repository> orbitdiff && cd orbitdiff && git checkout --detach v1.0.0
```

```bash
cd deploy && cp .env.example .env && chmod 600 .env
```

Edit `.env`. Set `ORBITDIFF_VERSION=v1.0.0` and `ORBITDIFF_REGISTRY=ghcr.io/<owner>`, in
lower case.

**3. Make the folders the containers write to.** They run as user 1000. Docker would create
missing folders as root, and the workers couldn't write to them.

```bash
mkdir -p data backups && sudo chown -R 1000:1000 data backups ../eval
```

**4. Start it.**

```bash
docker compose pull && docker compose up --detach
```

Check it from anywhere:

```bash
curl -s https://your-domain.example/api/health
```

**5. Sign in** with an address in `ORBITDIFF_ADMIN_EMAILS` and open `#/admin`.

The worker downloads the cloud model's weights the first time it reads imagery, about 55 MB
into `data/models`.

## Updating

```bash
deploy/update.sh v1.1.0
```

It checks out the tag, pulls the images and restarts what changed. Migrations run first, and
only go forward. To go back past a migration, restore a backup. Run it as the user that owns
the checkout, or `git` can't write to `eval/` after the `chown` above.

To let GitHub do this when a tag is pushed, set on the repository:

- Variables `DEPLOY_HOST`, `DEPLOY_USER` and `DEPLOY_PATH` (the checkout's absolute path).
- Secrets, in an environment named `production`: `DEPLOY_SSH_KEY`, a key that can log in
  and run Docker, and `DEPLOY_KNOWN_HOSTS`, from `ssh-keyscan your-machine`.

Without `DEPLOY_HOST` the deploy job is skipped and only the images are published.

## Backups

The `backup` service runs at 03:00 UTC. It dumps Postgres to `deploy/backups` and keeps 14
days. With a remote configured it also copies the dump to the bucket and mirrors `cubes`,
`masks` and `composites` there. The mirror is a copy of what's on disk, so something deleted
here is deleted there. Dumps are what's irreplaceable: rasters can be rebuilt from STAC.

Without a remote the dumps sit on the same disk as the database. A failed disk loses both,
and `docker compose logs backup` says so every night.

Run one now, and restore into a scratch database to see that it works:

```bash
docker compose exec backup backup.sh --once
```

```bash
docker compose exec db createdb -U orbitdiff restore_check
```

```bash
docker compose exec backup sh -c 'pg_restore --dbname=restore_check $(ls -t /backups/*.dump | head -1)'
```

To restore for real, stop the application, restore into the `orbitdiff` database, and start
it again. Fetch the dump from the bucket first if the machine is gone:

```bash
docker compose stop api worker scheduler
```

```bash
docker compose exec backup sh -c 'pg_restore --clean --if-exists --dbname=orbitdiff /backups/<dump>'
```

```bash
docker compose start api worker scheduler
```

`docker compose ps` shows `backup` as unhealthy once two days pass without a dump.

## Things to know

- **TiTiler stays private.** It reads any file it's asked for. Only Caddy has a published
  port (ADR-013).
- **Retraining** runs on the worker, between backfills, when the admin view flags the building
  model. A candidate takes about 20 minutes of CPU and is only registered. Promoting it is
  `docker compose exec worker orbitdiff promote --version N`, which needs the evaluation set
  verified.
- **Email can only be sent by what you configure here.** `deploy/compose.yml` has no default
  mail host, and development never reads this file.
