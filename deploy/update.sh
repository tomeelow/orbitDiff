#!/bin/sh
# Move this machine to a release: deploy/update.sh v1.2.0. Migrations run on the way up, and
# only forward: going back to a release older than a migration needs a restore.
set -eu

version="${1:?usage: deploy/update.sh <version tag>}"
cd "$(dirname "$0")/.."
git fetch --tags --quiet origin
git checkout --quiet --detach "refs/tags/$version"

cd deploy
# Rewritten in place, so .env keeps its owner and its mode (600).
updated=$(sed "s|^ORBITDIFF_VERSION=.*|ORBITDIFF_VERSION=$version|" .env)
printf '%s\n' "$updated" > .env

# The registry has this version's images if the release workflow pushed them and this machine
# may read them. If not, `up` builds the missing ones from the checkout, which is at the tag.
docker compose pull --quiet ||
    echo "update: not every image could be pulled; building what's missing here (the first build takes a while)" >&2
docker compose up --detach --remove-orphans
docker compose ps
