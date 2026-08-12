# Spike S1 — option B: audio to the composer over HTTP (output.harbor).
# Env: MUSIC_DIR, HARBOR_PORT, HARBOR_MOUNT

settings.log.stdout.set(true)
settings.log.file.set(false)
settings.log.level.set(3)

music_dir   = environment.get("MUSIC_DIR")
harbor_port = int_of_string(environment.get("HARBOR_PORT"))
mount       = environment.get("HARBOR_MOUNT")

pl = playlist(id="pl", mode="normal", reload_mode="watch", music_dir)

# crossfade re-introduces fallibility, so mksafe (fallback to silence) goes
# outermost: a media failure must never take the harbor mount down, because that
# is what the composer is connected to.
s = crossfade(duration=3., pl)
s = mksafe(s)

def log_track(m) =
  fn = m["filename"]
  print("SPIKE_TRACK t=#{time()} file=#{fn}")
end
s.on_track(synchronous=false, log_track)

enc = %mp3(bitrate=256, samplerate=44100, stereo=true)  # ENCODER_LINE

output.harbor(
  enc,
  id="harbor",
  port=harbor_port,
  mount=mount,
  burst=0,
  s
)
