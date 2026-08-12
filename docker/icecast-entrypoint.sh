#!/bin/sh
# Renders docker/icecast.xml into /etc/icecast.xml and runs Icecast.
#
# Two jobs the stock image cannot do:
#   1. Keep passwords out of the tracked config — they arrive as environment.
#   2. Emit one <mount> block per channel, each with its own fallback mount.
#
# Adding a channel must never restart Icecast: a restart takes every running
# compositor's audio input with it (docs/contracts/audio-transport.md). So the
# channel list lives in a bind-mounted file and SIGHUP re-renders + reloads.
#
#   docker kill -s HUP ambient-icecast
#
set -eu

TEMPLATE="${ICECAST_TEMPLATE:-/etc/ambient/icecast.xml}"
RENDERED="${ICECAST_CONFIG:-/etc/icecast.xml}"
MOUNTS_LIST="${ICECAST_MOUNTS_LIST:-/etc/ambient/channels/mounts.list}"
WEBROOT="${ICECAST_WEBROOT:-/usr/share/icecast/web}"
FALLBACK_DIR="${ICECAST_FALLBACK_DIR:-fallback}"

log()  { printf '[icecast-entrypoint] %s\n' "$*"; }
die()  { printf '[icecast-entrypoint] FATAL: %s\n' "$*" >&2; exit 1; }

[ -f "$TEMPLATE" ] || die "template not found at $TEMPLATE"

for var in ICECAST_SOURCE_PASSWORD ICECAST_ADMIN_PASSWORD ICECAST_RELAY_PASSWORD; do
  eval "value=\${$var:-}"
  [ -n "$value" ] || die "$var is empty. Set it in .env (openssl rand -hex 16)."
done

# Icecast has no XML escaping for us, and a password with & < > \" ' would produce
# an unparseable config that fails at start with a useless message.
for var in ICECAST_SOURCE_PASSWORD ICECAST_ADMIN_PASSWORD ICECAST_RELAY_PASSWORD; do
  eval "value=\${$var}"
  case "$value" in
    *'&'*|*'<'*|*'>'*|*'"'*|*"'"*)
      die "$var contains an XML metacharacter. Use openssl rand -hex 16." ;;
  esac
done

# Builds the <mount> blocks. One live mount per channel, each pointing at its own
# fallback file with fallback-override so listeners return when the source does.
render_mounts() {
  [ -f "$MOUNTS_LIST" ] || { log "no mounts list at $MOUNTS_LIST — fileserve only"; return 0; }
  while IFS= read -r raw; do
    name=$(printf '%s' "$raw" | tr -d ' \t\r' )
    case "$name" in ''|'#'*) continue ;; esac
    mount="/${name#/}"
    channel="${mount#/}"

    fallback="/${FALLBACK_DIR}/${channel}.mp3"
    if [ ! -f "${WEBROOT}${fallback}" ]; then
      fallback="/${FALLBACK_DIR}/default.mp3"
      [ -f "${WEBROOT}${fallback}" ] \
        || log "WARNING: no fallback file for ${mount}; a Liquidsoap restart will drop listeners"
    fi

    cat <<EOF
    <mount type="normal">
        <mount-name>${mount}</mount-name>
        <fallback-mount>${fallback}</fallback-mount>
        <fallback-override>1</fallback-override>
        <fallback-when-full>1</fallback-when-full>
        <public>0</public>
        <hidden>1</hidden>
    </mount>
EOF
  done < "$MOUNTS_LIST"
}

render() {
  mounts=$(render_mounts)
  MOUNT_BLOCKS="$mounts" \
  ICECAST_PORT="${ICECAST_PORT:-8081}" \
  ICECAST_ADMIN_USER="${ICECAST_ADMIN_USER:-admin}" \
  ICECAST_ADMIN_EMAIL="${ICECAST_ADMIN_EMAIL:-admin@localhost}" \
  ICECAST_MAX_CLIENTS="${ICECAST_MAX_CLIENTS:-64}" \
  ICECAST_MAX_SOURCES="${ICECAST_MAX_SOURCES:-16}" \
  ICECAST_LOG_LEVEL="${ICECAST_LOG_LEVEL:-3}" \
  awk '
    # Literal substitution — no sed, because & and \\ in a replacement are magic
    # to sed and a generated password would silently corrupt the config.
    function subst(line, tok, val,   i, out) {
      while ((i = index(line, tok)) > 0) {
        out = out substr(line, 1, i - 1) val
        line = substr(line, i + length(tok))
      }
      return out line
    }
    {
      if ($0 ~ /@CHANNEL_MOUNTS@/) { printf "%s\n", ENVIRON["MOUNT_BLOCKS"]; next }
      l = $0
      l = subst(l, "@SOURCE_PASSWORD@", ENVIRON["ICECAST_SOURCE_PASSWORD"])
      l = subst(l, "@RELAY_PASSWORD@",  ENVIRON["ICECAST_RELAY_PASSWORD"])
      l = subst(l, "@ADMIN_PASSWORD@",  ENVIRON["ICECAST_ADMIN_PASSWORD"])
      l = subst(l, "@ADMIN_USER@",      ENVIRON["ICECAST_ADMIN_USER"])
      l = subst(l, "@ADMIN_EMAIL@",     ENVIRON["ICECAST_ADMIN_EMAIL"])
      l = subst(l, "@PORT@",            ENVIRON["ICECAST_PORT"])
      l = subst(l, "@MAX_CLIENTS@",     ENVIRON["ICECAST_MAX_CLIENTS"])
      l = subst(l, "@MAX_SOURCES@",     ENVIRON["ICECAST_MAX_SOURCES"])
      l = subst(l, "@LOG_LEVEL@",       ENVIRON["ICECAST_LOG_LEVEL"])
      print l
    }
  ' "$TEMPLATE" > "${RENDERED}.tmp"
  mv "${RENDERED}.tmp" "$RENDERED"
  chmod 600 "$RENDERED"
}

render
MOUNT_COUNT=$(grep -c '<mount-name>' "$RENDERED" || true)
log "rendered $RENDERED with $MOUNT_COUNT channel mounts"

icecast -c "$RENDERED" -n &
ICECAST_PID=$!

# Re-render on SIGHUP, then hand Icecast its own SIGHUP to reload in place.
reload() { log "SIGHUP — re-rendering config"; render; kill -HUP "$ICECAST_PID" 2>/dev/null || true; }
trap reload HUP

# Forward shutdown so `docker compose down` is not a 10s SIGKILL wait.
shutdown() { trap - HUP INT TERM; kill -TERM "$ICECAST_PID" 2>/dev/null || true; wait "$ICECAST_PID" 2>/dev/null || true; }
trap shutdown INT TERM EXIT

# `wait` returns early on a trapped signal, so loop until the child is gone.
while kill -0 "$ICECAST_PID" 2>/dev/null; do wait "$ICECAST_PID" 2>/dev/null || true; done
