# Nightly backups (deploy/backup.sh). The Postgres client's major version matches the server.
FROM alpine:3.23.3
RUN apk add --no-cache postgresql17-client rclone
COPY deploy/backup.sh /usr/local/bin/backup.sh
# rclone's remote comes from RCLONE_CONFIG_BACKUP_* in the environment; there is no file.
ENV RCLONE_CONFIG=/dev/null
USER 1000:1000
CMD ["backup.sh"]
