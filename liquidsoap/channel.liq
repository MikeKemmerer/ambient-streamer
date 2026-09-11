# Channel audio engine.
#
# Liquidsoap is only a *source client*. The compositor's HTTP connection
# terminates on Icecast, which never restarts, so this process may come and go
# without the compositor ever seeing a break — see docs/contracts/audio-transport.md.
#
# Env (all optional except CHANNEL_MOUNT / ICECAST_SOURCE_PASSWORD):
#   CHANNEL_NAME              label used in logs and the Icecast stream name
#   PLAYLIST_FILE             absolute path to playlist.m3u   (/media/channel/playlist.m3u)
#   ICECAST_HOST              icecast
#   ICECAST_PORT              8081
#   CHANNEL_MOUNT             /lofi                           REQUIRED
#   ICECAST_SOURCE_PASSWORD                                   REQUIRED
#   CROSSFADE_SECONDS         5.0   (0 disables crossfade)
#   LIQ_TELNET_BIND           0.0.0.0   container-internal only, NEVER published
#   LIQ_TELNET_PORT           1234
#   LIQ_LOG_LEVEL             3

def env(name, default) =
  v = environment.get(name)
  if v == "" then default else v end
end

channel_name  = env("CHANNEL_NAME", "channel")
playlist_file = env("PLAYLIST_FILE", "/media/channel/playlist.m3u")
ice_host      = env("ICECAST_HOST", "icecast")
ice_port      = int_of_string(env("ICECAST_PORT", "8081"))
ice_mount     = env("CHANNEL_MOUNT", "")
ice_password  = env("ICECAST_SOURCE_PASSWORD", "")
crossfade_s   = float_of_string(env("CROSSFADE_SECONDS", "5.0"))
telnet_bind   = env("LIQ_TELNET_BIND", "0.0.0.0")
telnet_port   = int_of_string(env("LIQ_TELNET_PORT", "1234"))
log_level     = int_of_string(env("LIQ_LOG_LEVEL", "3"))

settings.log.stdout.set(true)
settings.log.file.set(false)
settings.log.level.set(log_level)

def logf(event, fields) =
  log.important(label="ambient", "event=#{event} channel=#{channel_name} #{fields}")
end

if ice_mount == "" then
  log.severe(label="ambient", "event=fatal reason=CHANNEL_MOUNT_unset")
  shutdown()
end
if ice_password == "" then
  log.severe(label="ambient", "event=fatal reason=ICECAST_SOURCE_PASSWORD_unset")
  shutdown()
end

# The telnet socket is a full command interface. It is reachable only on the
# channel's compose network; publishing this port to the host is a remote shell.
settings.server.telnet.set(true)
settings.server.telnet.bind_addr.set(telnet_bind)
settings.server.telnet.port.set(telnet_port)

# A playlist FILE, not a directory: it is the only form that carries explicit
# order and mixes the two media trees. See docs/contracts/media-selection.md.
pl = playlist(id="playlist", mode="normal", reload_mode="watch", playlist_file)

# Lets the operator put one specific track on air. `playlist` has no "play this
# one" command - its telnet verbs only move an internal cursor and leave the
# audio where it was, measured on a live channel - so a request queue in front
# of it is the only way to honour a choice.
#
# track_sensitive=false so a push interrupts the current track rather than
# waiting for it to end; that is what an operator clicking a track means.
requests = request.queue(id="queue")
programme = fallback(id="programme", track_sensitive=false, [requests, pl])

# mksafe must be OUTERMOST: a fallible source reaching output.icecast drops the
# mount, which is the one failure Icecast cannot paper over.
faded = if crossfade_s > 0. then crossfade(id="xfade", duration=crossfade_s, programme) else programme end
effects = request.queue(id="soundboard")
radio = mksafe(limit(smooth_add(duration=0.15, p=0.35, normal=faded, special=effects)))

current = ref("")
started = ref(time())

def next_file() =
  try
    list.hd(default="", pl.remaining_files())
  catch _ do
    ""
  end
end

# on_track does not report the first track of a freshly started source, and the
# mksafe blank fallback carries no filename at all, so the ref is seeded from
# the playlist rather than left empty until the first transition.
def seed_current() =
  if current() == "" then
    fn = next_file()
    if fn != "" then
      current := fn
      started := time()
      logf("track_seeded", "file='#{fn}'")
    end
  end
end

def log_track(m) =
  fn = m["filename"]
  if fn == "" then
    held = current()
    logf("track_unnamed", "held='#{held}'")
  else
    ti = m["title"]
    ar = m["artist"]
    current := fn
    started := time()
    logf("track", "file='#{fn}' title='#{ti}' artist='#{ar}'")
  end
end
# Tracked below the crossfade, not on the output: crossfade merges a mid-track
# switch into the track it is already playing, so a queue takeover produces no
# track mark at `radio` at all - measured, the switch happened and the reported
# track never moved. The cost is that this fires up to `crossfade_s` early,
# because crossfade reads that far ahead.
programme.on_track(synchronous=false, log_track)

seed_current()

server.register(
  namespace="ambient", description="Current track, next track and buffer state.",
  usage="status", "status",
  fun (_) -> begin
    seed_current()
    cur = current()
    nxt = next_file()
    el  = time() - started()
    rem = radio.remaining()
    rdy = radio.is_ready()
    "channel=#{channel_name} mount=#{ice_mount} current='#{cur}' next='#{nxt}' " ^
    "elapsed=#{el} remaining=#{rem} ready=#{rdy}"
  end
)

server.register(
  namespace="ambient", description="Absolute path of the track now playing.",
  usage="current", "current",
  fun (_) -> begin
    seed_current()
    current()
  end
)

server.register(
  namespace="ambient", description="Absolute path of the next track.",
  usage="next", "next", fun (_) -> next_file()
)

# MP3 only. Ogg/Vorbis/FLAC were measured unrecoverable across a reconnect or a
# fallback-mount switch: 60-67% silence, CRC mismatches, non-monotonic DTS.
# 44.1 kHz is the single clock the whole pipeline shares and is not negotiable.
output.icecast(
  %mp3(bitrate=256, samplerate=44100, stereo=true),
  id="icecast",
  host=ice_host,
  port=ice_port,
  mount=ice_mount,
  password=ice_password,
  name="#{channel_name}",
  description="ambient-streamer #{channel_name}",
  on_connect=fun () -> logf("icecast_connected", "host=#{ice_host} port=#{ice_port} mount=#{ice_mount}"),
  on_disconnect=fun () -> logf("icecast_disconnected", "mount=#{ice_mount}"),
  radio
)

logf("startup",
     "playlist='#{playlist_file}' icecast=#{ice_host}:#{ice_port}#{ice_mount} " ^
     "crossfade_s=#{crossfade_s} telnet=#{telnet_bind}:#{telnet_port}")
