# Task 0 — does playlist(reload_mode="watch") fire on a playlist FILE?
# S1 only proved the directory case. media-selection.md now depends on the file
# case, and flags it UNVERIFIED. This measures watch-vs-poll and rename-vs-rewrite.
# Env: PLAYLIST_FILE, RELOAD_MODE (watch|seconds), RELOAD_SECONDS, PHASE

settings.log.stdout.set(true)
settings.log.file.set(false)
settings.log.level.set(4)

playlist_file = environment.get("PLAYLIST_FILE")
reload_mode   = environment.get("RELOAD_MODE")
reload_secs   = int_of_string(environment.get("RELOAD_SECONDS"))
phase         = environment.get("PHASE")

pl =
  if reload_mode == "watch" then
    playlist(id="pl", mode="normal", reload_mode="watch", playlist_file)
  else
    playlist(id="pl", mode="normal", reload_mode="seconds", reload=reload_secs,
             playlist_file)
  end

s = mksafe(pl)

def log_track(m) =
  fn = m["filename"]
  print("T0_TRACK phase=#{phase} t=#{time()} file=#{fn}")
end
s.on_track(synchronous=false, log_track)

print("T0_START phase=#{phase} mode=#{reload_mode} reload=#{reload_secs} t=#{time()}")

output.dummy(s)
