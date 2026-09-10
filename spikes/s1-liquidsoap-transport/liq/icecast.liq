# Spike S1 — option C: audio to the composer via a long-lived Icecast relay.
# Liquidsoap is a *source client* here, so it can come and go without the
# composer's HTTP connection ever breaking.
# Env: MUSIC_DIR, ICE_HOST, ICE_PORT, ICE_MOUNT, ICE_PASSWORD

settings.log.stdout.set(true)
settings.log.file.set(false)
settings.log.level.set(3)

music_dir    = environment.get("MUSIC_DIR")
ice_host     = environment.get("ICE_HOST")
ice_port     = int_of_string(environment.get("ICE_PORT"))
ice_mount    = environment.get("ICE_MOUNT")
ice_password = environment.get("ICE_PASSWORD")

pl = playlist(id="pl", mode="normal", reload_mode="watch", music_dir)
s  = mksafe(crossfade(duration=3., pl))

def log_track(m) =
  fn = m["filename"]
  print("SPIKE_TRACK t=#{time()} file=#{fn}")
end
s.on_track(synchronous=false, log_track)

output.icecast(
  %mp3(bitrate=256, samplerate=44100, stereo=true),
  id="ice",
  host=ice_host,
  port=ice_port,
  mount=ice_mount,
  password=ice_password,
  name="ambient-streamer spike",
  s
)
