#!/bin/sh
# Deploys a new version tag by itself: cron runs this every five minutes (deploy/setup.sh
# installs it). Logs: journalctl -t orbitdiff-autoupdate
#
# Each tag is deployed once. A rollback with deploy/update.sh stays put until a newer tag is
# pushed, and a failed update isn't retried: fix it, then run update.sh by hand.
set -eu
cd "$(dirname "$0")"

exec 9>.autoupdate.lock
flock -n 9 || exit 0 # the previous run is still updating

latest=$(git ls-remote --tags --refs origin 'v*' | sed 's|.*refs/tags/||' | sort -V | tail -n 1)
seen=$(cat .autoupdate-tag 2>/dev/null || sed -n 's|^ORBITDIFF_VERSION=||p' .env)
[ -n "$latest" ] && [ "$latest" != "$seen" ] || exit 0

# The release workflow publishes the images a while after the tag is pushed. Updating before
# then would build them all here.
if ! ORBITDIFF_VERSION="$latest" docker compose pull --quiet >/dev/null 2>&1; then
    echo "$latest is tagged; waiting for its images"
    exit 0
fi
printf '%s\n' "$latest" >.autoupdate-tag
echo "updating from $seen to $latest"
exec ./update.sh "$latest"
