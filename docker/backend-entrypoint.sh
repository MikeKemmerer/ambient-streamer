#!/bin/sh
# Entrypoint for the ambient-streamer control plane.
#
# Does three things root is needed for, then stops being root:
#
#   1. aligns the container user with PUID/PGID, so the compose files and logs
#      this process writes into the repo stay editable on the host
#   2. joins the Docker socket's group, whose GID differs per host and so
#      cannot be baked into the image
#   3. proves the daemon actually answers as the unprivileged user, because
#      "permission denied on /var/run/docker.sock" at the first channel start
#      is a much worse place to find out
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
APP_USER=ambient
SOCKET="${DOCKER_HOST_SOCKET:-/var/run/docker.sock}"

log()  { printf '[entrypoint] %s\n' "$*"; }
die()  { printf '[entrypoint] FATAL: %s\n' "$*" >&2; exit 1; }

[ -S "$SOCKET" ] || die "$SOCKET is not a socket. The control plane drives
  'docker compose', so mount it:  -v /var/run/docker.sock:/var/run/docker.sock"

[ -n "${AMBIENT_ROOT:-}" ] || die "AMBIENT_ROOT is unset. It must be the repository
  path as the HOST sees it."

# The compose files this process renders name host paths, and the daemon
# resolves a bind source on the host, not in here. Mounting the repository at
# the identical path is what makes the two agree. A mismatch does not fail
# here — it fails later as a channel that starts with empty media directories.
[ -d "$AMBIENT_ROOT" ] || die "AMBIENT_ROOT=$AMBIENT_ROOT is not present in this
  container. Bind-mount the repository at its own host path:
    -v \"\$AMBIENT_ROOT:\$AMBIENT_ROOT\""
[ -f "$AMBIENT_ROOT/docker/compose.channel.yml.j2" ] \
  || die "$AMBIENT_ROOT does not look like the repository (no docker/compose.channel.yml.j2)"

if [ "$(id -u)" -eq 0 ]; then
  getent group "$PGID" >/dev/null 2>&1 || groupadd -g "$PGID" "$APP_USER"

  getent passwd "$PUID" >/dev/null 2>&1 \
    || useradd -u "$PUID" -g "$PGID" -M -d /run/ambient -s /usr/sbin/nologin "$APP_USER"
  USER_NAME="$(getent passwd "$PUID" | cut -d: -f1)"

  SOCK_GID="$(stat -c '%g' "$SOCKET")"
  if [ "$SOCK_GID" = "0" ]; then
    die "$SOCKET is group root. Dropping privileges would leave this process
  unable to reach the daemon. Add the host's docker group to the socket, or
  run the container with 'user: root' and accept that the API handler and the
  socket share a uid."
  fi
  getent group "$SOCK_GID" >/dev/null 2>&1 || groupadd -g "$SOCK_GID" dockersock
  SOCK_GROUP="$(getent group "$SOCK_GID" | cut -d: -f1)"
  id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx "$SOCK_GROUP" \
    || usermod -aG "$SOCK_GROUP" "$USER_NAME"

  for dir in /run/ambient "${AMBIENT_LOG_DIR:-/var/log/ambient}"; do
    [ -d "$dir" ] || mkdir -p "$dir"
    chown "$PUID:$PGID" "$dir" 2>/dev/null || true
  done

  # Probe as the user that will actually run, not as root.
  gosu "$USER_NAME" docker version --format '{{.Server.Version}}' >/dev/null 2>&1 \
    || die "the Docker daemon did not answer as uid $PUID (socket group $SOCK_GROUP/$SOCK_GID).
  Without it this process cannot start a channel."
  log "docker $(gosu "$USER_NAME" docker version --format '{{.Server.Version}}') reachable as uid $PUID"

  exec gosu "$USER_NAME" "$@"
fi

# Already non-root — compose set `user:`. Nothing to drop.
exec "$@"
