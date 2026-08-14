"""A public directory of the local feeds, for listeners rather than operators.

Served without a token, and deliberately so: it sits on the same LAN port as the
HLS itself, which MediaMTX already serves unauthenticated. It exposes station
names and URLs — never a stream key, a path, or any control.

`active` means the relay says the path is publishing, not that a channel is
configured. A directory that lists a station you cannot tune to is worse than an
empty one.

The program path is never listed. It is the YouTube feed, and it is the one path
the publisher hook reads.
"""

from __future__ import annotations

import html
from typing import Any

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from fastapi import APIRouter

from ..main import AppState
from .deps import get_state

router = APIRouter(tags=["directory"])

# The renditions a listener can tune to. `preview` is the operator's 360p feed
# and is deliberately absent: this is a directory of stations, not of tooling.
LISTENABLE = ("video", "audio")

_KIND = {
    "video": "video",
    "audio": "audio only",
}


async def _stations(state: AppState, request: Request) -> list[dict[str, Any]]:
    relay = await state.relay_paths()
    if not relay:
        return []

    # Built from the relay listing and the channel config only. This endpoint is
    # unauthenticated, so it must not do work an anonymous caller could amplify:
    # reading now.json would be a `docker exec` per channel per page load.
    base = f"{request.url.scheme}://{request.headers.get('host', '')}"
    stations: list[dict[str, Any]] = []
    for name in sorted(state.names()):
        try:
            channel = state.channel(name, resolve_media=False)
        except Exception:  # a broken channel must not blank the whole directory
            continue

        feeds = [
            {"kind": _KIND[rendition], "url": f"{base}/{name}/{rendition}/index.m3u8"}
            for rendition in LISTENABLE
            if (relay.get(f"{name}/{rendition}") or {}).get("ready")
        ]
        if not feeds:
            continue

        stations.append(
            {"name": name, "genre": channel.config.genre or "", "feeds": feeds}
        )
    return stations


@router.get("/directory.json")
async def directory_json(
    request: Request, state: AppState = Depends(get_state)
) -> dict[str, Any]:
    return {"stations": await _stations(state, request)}


# Not `/`: the operator UI is mounted there, and an explicit route would win
# against it. nginx maps the public root onto this path.
@router.get("/directory", response_class=HTMLResponse)
async def directory_page(
    request: Request, state: AppState = Depends(get_state)
) -> HTMLResponse:
    stations = await _stations(state, request)
    return HTMLResponse(_render(stations), headers={"Cache-Control": "no-store"})


def _render(stations: list[dict[str, Any]]) -> str:
    """Hand-built HTML with everything escaped; no template engine on this path."""
    if stations:
        body = "\n".join(_card(station) for station in stations)
    else:
        body = (
            '<p class="empty">No local stations are on air right now. '
            "A channel appears here once it is running with an internal video or "
            "audio feed.</p>"
        )
    return _PAGE.replace("{{body}}", body).replace("{{count}}", str(len(stations)))


def _card(station: dict[str, Any]) -> str:
    name = html.escape(station["name"])
    genre = html.escape(station["genre"])
    rows = "\n".join(
        f'<li><span class="kind">{html.escape(feed["kind"])}</span>'
        f'<code>{html.escape(feed["url"])}</code>'
        f'<button type="button" data-url="{html.escape(feed["url"], quote=True)}">Copy</button></li>'
        for feed in station["feeds"]
    )
    return f"""<article class="station">
  <h2>{name}</h2>
  {f'<p class="genre">{genre}</p>' if genre else ''}
  <ul class="feeds">{rows}</ul>
</article>"""


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Local stations</title>
<style>
  :root { color-scheme: dark; --bg:#0e1116; --panel:#161b22; --line:#262d36;
          --fg:#e6edf3; --mute:#8b949e; --accent:#4fc3f7; }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px 16px 48px; background:var(--bg); color:var(--fg);
         font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
  .wrap { max-width: 720px; margin: 0 auto; }
  h1 { font-size:20px; margin:0 0 2px; }
  .lead { color:var(--mute); font-size:13px; margin:0 0 24px; }
  .station { background:var(--panel); border:1px solid var(--line);
             border-radius:10px; padding:14px 16px; margin-bottom:12px; }
  .station h2 { font-size:16px; margin:0; }
  .genre { color:var(--mute); font-size:12px; margin:2px 0 0; }
  .now { color:var(--accent); font-size:12px; margin:6px 0 0; }
  .feeds { list-style:none; margin:10px 0 0; padding:0;
           display:flex; flex-direction:column; gap:6px; }
  .feeds li { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .kind { flex:none; width:74px; font-size:11px; text-transform:uppercase;
          letter-spacing:.06em; color:var(--mute); }
  code { flex:1; min-width:220px; word-break:break-all; font-size:12px;
         padding:4px 7px; background:var(--bg); border:1px solid var(--line);
         border-radius:5px; }
  button { flex:none; font:inherit; font-size:12px; padding:4px 10px; color:var(--fg);
           background:var(--panel); border:1px solid var(--line); border-radius:5px;
           cursor:pointer; }
  button:hover { border-color:var(--accent); }
  .empty { color:var(--mute); }
  footer { color:var(--mute); font-size:12px; margin-top:24px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Local stations</h1>
  <p class="lead">{{count}} on air. Paste a URL into VLC &rsaquo; Open Network Stream,
     or any HLS player on this network.</p>
  {{body}}
  <footer>This page refreshes every 30 seconds.</footer>
</div>
<script>
  document.addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-url]');
    if (!button) return;
    try {
      await navigator.clipboard.writeText(button.dataset.url);
      button.textContent = 'Copied';
    } catch {
      // Clipboard access is refused on insecure origins, which is where this
      // page lives; select the URL so it can still be copied by hand.
      const range = document.createRange();
      range.selectNodeContents(button.previousElementSibling);
      const selection = getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      button.textContent = 'Selected';
    }
    setTimeout(() => { button.textContent = 'Copy'; }, 1500);
  });
  // Reloading keeps "active" honest without holding a connection open.
  setTimeout(() => location.reload(), 30000);
</script>
</body>
</html>
"""
