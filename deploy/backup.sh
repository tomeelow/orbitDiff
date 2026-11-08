#!/bin/sh
# Every night: a Postgres dump, kept here and copied off the machine if a remote is
# configured, and a mirror of the rasters. Rasters can be rebuilt from STAC, slowly; the
# database can't be rebuilt at all (docs/deploy.md).
set -eu

keep_days="${ORBITDIFF_BACKUP_KEEP_DAYS:-14}"
hour_utc=3

backup_once() {
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    partial="/backups/.orbitdiff-$stamp.part"
    dump="/backups/orbitdiff-$stamp.dump"
    pg_dump --format=custom --file="$partial"  # connection from PG* in the environment
    pg_restore --list "$partial" >/dev/null    # an unreadable dump is not a backup
    mv "$partial" "$dump"
    find /backups -name 'orbitdiff-*.dump' -mtime +"$keep_days" -delete
    echo "backup: wrote $dump ($(du -h "$dump" | cut -f1))"

    if [ -z "${RCLONE_CONFIG_BACKUP_TYPE:-}" ]; then
        echo "backup: no remote configured; the dump is on this machine only" >&2
        return
    fi
    remote="backup:${ORBITDIFF_BACKUP_BUCKET:?set it with the remote}"
    rclone copy "$dump" "$remote/db/"
    rclone delete --min-age "${keep_days}d" "$remote/db/"
    for folder in cubes masks composites; do
        if [ -d "/data/$folder" ]; then
            rclone sync "/data/$folder" "$remote/$folder"
        fi
    done
    echo "backup: copied to the remote"
}

if [ "${1:-}" = "--once" ]; then
    backup_once
    exit
fi

while :; do
    seconds_today=$(($(date -u +%s) % 86400))
    sleep $(((hour_utc * 3600 - seconds_today + 86400 - 1) % 86400 + 1))
    # A separate process, so `set -e` still stops it at the first failure.
    "$0" --once || echo "backup: FAILED at $(date -u +%FT%TZ)" >&2
done
