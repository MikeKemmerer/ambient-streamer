#!/bin/sh
# Shared drop-privileges entrypoint for the ambient-streamer channel containers.
#
# Runs as root only long enough to align the container user with PUID/PGID so
# files written to the log mount stay editable on the host, then execs the real
# process as an unprivileged user.
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
APP_USER=ambient

die() { printf '[entrypoint] FATAL: %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then
  if ! getent group "$PGID" >/dev/null 2>&1; then
    groupadd -g "$PGID" "$APP_USER" 2>/dev/null || addgroup -g "$PGID" "$APP_USER"
  fi
  GROUP_NAME="$(getent group "$PGID" | cut -d: -f1)"

  if ! getent passwd "$PUID" >/dev/null 2>&1; then
    useradd -u "$PUID" -g "$PGID" -M -d /run/ambient -s /usr/sbin/nologin "$APP_USER" 2>/dev/null \
      || adduser -u "$PUID" -G "$GROUP_NAME" -H -h /run/ambient -s /sbin/nologin -D "$APP_USER"
  fi
  USER_NAME="$(getent passwd "$PUID" | cut -d: -f1)"

  # Only the writable surfaces. /media/* are read-only mounts by design.
  for dir in /run/ambient /var/log/ambient; do
    [ -d "$dir" ] || mkdir -p "$dir"
    chown "$PUID:$PGID" "$dir" 2>/dev/null || true
  done

  exec gosu "$USER_NAME" "$@"
fi

# Already non-root (compose `user:` was set) — nothing to drop.
exec "$@"
