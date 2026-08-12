# Spike S1 — option A: audio to the composer over a named pipe.
# Env: MUSIC_DIR, FIFO_PATH

settings.log.stdout.set(true)
settings.log.file.set(false)
settings.log.level.set(3)

music_dir = environment.get("MUSIC_DIR")
fifo_path = environment.get("FIFO_PATH")

pl = playlist(id="pl", mode="normal", reload_mode="watch", music_dir)
s  = mksafe(crossfade(duration=3., pl))

output.file(
  %mp3(bitrate=256, samplerate=44100, stereo=true),
  append=true,
  fifo_path,
  s
)
