#!/bin/sh
set -eu
mkdir -p /app/data
# Render mounts the persistent disk at runtime; assign it to the unprivileged worker on each boot.
chown -R 10001:10001 /app/data
exec setpriv --reuid=10001 --regid=10001 --init-groups "$@"
