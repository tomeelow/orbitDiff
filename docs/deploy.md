# Deploying Orbit Diff

One machine running `deploy/compose.yml`, behind Caddy. `deploy/setup.sh` takes a fresh
machine to a running, checked site.

What has been run, and where: the setup script, the stack, sign-in through Caddy, a backfill,
tiles, backups and a restore have all run on a laptop, with a local certificate and a local
mail catcher. Nothing here has touched a real domain, a real email provider or a real bucket:
those need your accounts.

## What you need to provide

| What | Used for | Where it goes |
|---|---|---|
| A Linux **x86-64 or arm64 machine** with Docker, SSH access, about 40 GB of disk and 4 GB of memory or more | Runs everything | n/a |
| A **domain** with its A record pointing at the machine, ports 80 and 443 open | Caddy gets the certificate by itself | `ORBITDIFF_DOMAIN` |
| An **email provider**'s SMTP host, username and password, and a verified sender | Sign-in links and alerts | `ORBITDIFF_SMTP_*`, `ORBITDIFF_MAIL_FROM` |
| An **S3-compatible bucket** and an access key (optional, strongly advised) | Backups that survive losing the machine | `RCLONE_CONFIG_BACKUP_*`, `ORBITDIFF_BACKUP_BUCKET` |

That's all. Earth Search needs no key and Planetary Computer issues an anonymous token. The
database password and the signing key are generated on the machine.

### On Oracle Cloud's free tier

An Always Free Ampere (arm64) instance is enough. One thing differs from other providers:
Oracle's Ubuntu images reject everything but SSH in the machine's own firewall, on top of the
network's security list. Open ports 80 and 443 in both: an ingress rule on the subnet's
security list, and on the machine

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp -m multiport --dports 80,443 -j ACCEPT
```

```bash
sudo netfilter-persistent save
```

On memory: the stack used about 1.6 GB while a backfill was reading passes. The cloud model
and detection add to that, so 8 GB leaves headroom. Disk: ten years of one area is under
500 MB.

## First deploy

**1. Point the domain at the machine,** and give the provider time to verify your sender
domain. Both take a while to spread, so start them first.

**2. On the machine,** install Docker ([docs.docker.com/engine/install](https://docs.docker.com/engine/install/)),
then:

```bash
git clone https://github.com/<owner>/orbitDiff.git orbitdiff && cd orbitdiff
```

```bash
deploy/setup.sh
```

It asks for the domain, your email address, and the provider's SMTP host, username, password
and sender address. Then it:

- writes `deploy/.env` (mode 600) and generates `POSTGRES_PASSWORD` and `ORBITDIFF_SIGNING_KEY`;
- makes `deploy/data` and `deploy/backups`, owned by the user the containers run as;
- checks out the newest version tag and starts the stack;
- runs `orbitdiff doctor` inside it, which checks the database and its migrations, the
  queue, the data folder, the tile server, the imagery catalog and the sign-in to your email
  provider.

To answer nothing, fill in `deploy/.env` first (copy `deploy/.env.example`); the script only
asks for what's empty. It's safe to run again: it keeps every value the file has.

**3. Sign in** at `https://your-domain` with the address you gave, and open `#/admin`. To see
an email arrive before that:

```bash
cd deploy && docker compose exec api orbitdiff doctor --email you@example.com
```

The worker downloads the cloud model's weights the first time it reads imagery, about 55 MB
into `data/models`.

### Where the images come from

Pushing a version tag runs `.github/workflows/release.yml`, which builds the four images for
x86-64 and arm64 and pushes them to `ghcr.io/<owner>/`. `main` never deploys itself.

```bash
git tag v1.1.0 && git push origin v1.1.0
```

The machine pulls them if it may. If the four `orbitdiff-*` packages are private, either make
them public (each package's settings on GitHub) or sign the machine in with a token that has
`read:packages`:

```bash
docker login ghcr.io
```

If it can't pull them, nothing breaks: `docker compose up` builds the missing images from the
checkout, which is at the same tag. The first build takes a while, mostly PyTorch for the
worker.

## Who can use it

Left as it is, anyone with an email address can sign in and draw up to five areas
(`[aoi] max_per_user` in `config/pipeline.toml`). Each area is hours of the worker and a few
gigabytes read from the catalog. To keep it to people you know, list them in `deploy/.env`
and restart:

```bash
ORBITDIFF_INVITED_EMAILS=["friend@example.com","colleague@example.com"]
```

```bash
docker compose up --detach
```

Admins can always sign in, and have no cap on areas.

## Updating

```bash
deploy/update.sh v1.1.0
```

It checks out the tag, pulls or builds the images and restarts what changed. Migrations run
first, and only go forward. To go back past a migration, restore a backup. Run it as the user
that owns the checkout, or `git` can't write to `eval/`.

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

## When something is wrong

```bash
docker compose exec api orbitdiff doctor
```

Each line is one check, and a failed one names the settings to look at. After changing
`deploy/.env`, `docker compose up --detach` restarts what the change affects.

- **No certificate:** the domain doesn't point here yet, or ports 80 and 443 are closed.
  `docker compose logs caddy` says which.
- **Sign-in emails don't arrive:** `doctor --email` shows whether the provider took the
  message. If it did, look in spam and at the provider's own log: the sender's domain needs
  its SPF and DKIM records.
- **An area stays "waiting to start":** `docker compose logs worker`.

## Things to know

- **TiTiler stays private.** It reads any file it's asked for. Only Caddy has a published
  port (ADR-013).
- **Deleting is final.** An area or an account deleted in the app is gone from the database
  and the disk at once, from the bucket's raster mirror at the next backup, and from the
  dumps once they age out (ADR-015).
- **Retraining** runs on the worker, between backfills, when the admin view flags the building
  model. A candidate takes about 20 minutes of CPU and is only registered. Promoting it is
  `docker compose exec worker orbitdiff promote --version N`, which needs the evaluation set
  verified.
- **Email can only be sent by what you configure here.** `deploy/compose.yml` has no default
  mail host, and development never reads this file.
