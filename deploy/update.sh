#!/bin/sh
# Move this machine to a release: deploy/update.sh v1.2.0. Migrations run on the way up, and
# only forward: going back to a release older than a migration needs a restore.
set -eu

version="${1:?usage: deploy/update.sh <version tag>}"
cd "$(dirname "$0")/.."
git fetch --tags --quiet origin
git checkout --quiet --detach "refs/tags/$version"

cd deploy
sed -i "s|^ORBITDIFF_VERSION=.*|ORBITDIFF_VERSION=$version|" .env
docker compose pull --quiet
docker compose up --detach --remove-orphans
docker compose ps
