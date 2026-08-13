"""Proxy the relay's low-resolution operator preview.

The relay publishes no host port, so a browser cannot reach it directly, and the
URL the API used to hand out named an internal Docker host that resolves nowhere
outside the compose network. Proxying puts the preview on the one published port
and behind the same token as everything else.

This is an outbound fetch driven by a path from the browser, so the upstream URL
is built only from the configured relay base and a validated channel and file
name. Nothing the caller sends can change the host, the port or the scheme.
"""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, Response

from ..main import ApiError, AppState
from .deps import Authed

LOG = logging.getLogger("ambient.api.preview")

router = APIRouter(tags=["preview"])

# One path component of an HLS playlist or segment. No separators, so a crafted
# name cannot climb out of the channel's own prefix on the relay.
_SEGMENT = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# `..` matches the pattern above, because a dot is legitimate in a segment name.
# It has to be excluded by name: the router decodes %2f before this sees it, so
# `..%2f..%2fetc%2fpasswd` arrives as ordinary path components.
_RESERVED = {".", ".."}

_CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".mp4": "video/mp4",
    ".m4s": "video/iso.segment",
    ".ts": "video/mp2t",
}

# A 360p segment is a few hundred KB; this only exists so a misbehaving upstream
# cannot make the control plane allocate without bound.
_MAX_BYTES = 12 * 1024 * 1024
_TIMEOUT = 15.0


def _content_type(name: str) -> str:
    for suffix, value in _CONTENT_TYPES.items():
        if name.endswith(suffix):
            return value
    return "application/octet-stream"


def _fetch(url: str) -> tuple[int, bytes, str]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            body = response.read(_MAX_BYTES + 1)
            if len(body) > _MAX_BYTES:
                return 502, b"", ""
            return response.status, body, response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, b"", ""
    except (urllib.error.URLError, TimeoutError, OSError):
        return 504, b"", ""


@router.get("/{channel}/preview/{path:path}")
async def preview_asset(channel: str, path: str, state: AppState = Authed) -> Response:
    """Serve one playlist or segment of a channel's preview rendition."""
    # Resolving the channel is the authorization check: an unknown name is a 404
    # before any outbound request is made.
    state.channel(channel, resolve_media=False)

    parts = [part for part in path.split("/") if part]
    if not parts or any(
        part in _RESERVED or not _SEGMENT.match(part) for part in parts
    ):
        raise ApiError(400, "invalid_preview_path", f"{path!r} is not a preview asset")

    base = state.workspace.ambient.relay.hls.rstrip("/")
    url = f"{base}/{urllib.parse.quote(channel)}/preview/" + "/".join(
        urllib.parse.quote(part) for part in parts
    )

    status, body, upstream_type = await asyncio.to_thread(_fetch, url)
    if status == 404:
        raise ApiError(404, "preview_not_ready", "the relay has no preview for this channel yet")
    if status >= 400 or not body:
        raise ApiError(503, "preview_unavailable", f"the relay answered {status}")

    name = parts[-1]
    headers = {"Cache-Control": "no-store"} if name.endswith(".m3u8") else {"Cache-Control": "max-age=10"}
    return Response(
        content=body,
        media_type=upstream_type or _content_type(name),
        headers=headers,
    )
