#!/usr/bin/env python3
"""Slideshow frame producer for the compositor's ``-f image2pipe`` input.

An FFmpeg input list is fixed at launch, so the image set can only change if a
separate process owns it. This producer owns slide order, hold and crossfade and
writes JPEG frames to stdout; the compositor never restarts.

stdout carries frames and nothing else. All logging goes to stderr.

See docs/contracts/slideshow.md, media-selection.md and zmq-control.md.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import queue
import random
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

from PIL import Image

import nowstate

LOG = sys.stderr


def log(event: str, **fields: object) -> None:
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    print(f"slideshow event={event} {parts}", file=LOG, flush=True)


def env_str(name: str, default: str) -> str:
    value = os.environ.get(name, "")
    return value if value else default


def env_float(name: str, default: float) -> float:
    try:
        return float(env_str(name, str(default)))
    except ValueError:
        log("bad_env", name=name, fallback=default)
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(env_str(name, str(default)))
    except ValueError:
        log("bad_env", name=name, fallback=default)
        return default


# --------------------------------------------------------------------------
# image list
# --------------------------------------------------------------------------

def read_list(path: str) -> list[str]:
    """One absolute path per line. No timings — see media-selection.md."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read().splitlines()
    except OSError as exc:
        log("list_unreadable", path=path, error=type(exc).__name__)
        return []
    slides: list[str] = []
    for line in raw:
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if not os.path.isabs(entry):
            log("list_entry_not_absolute", entry=repr(entry))
            continue
        slides.append(entry)
    return slides


def fit(path: str, width: int, height: int) -> Image.Image:
    """Cover-fit to exactly WxH. A mid-stream size change breaks the graph."""
    with Image.open(path) as opened:
        img = opened.convert("RGB")
    src_w, src_h = img.size
    scale = max(width / src_w, height / src_h)
    scaled = (max(width, round(src_w * scale)), max(height, round(src_h * scale)))
    img = img.resize(scaled, Image.LANCZOS)
    left = (scaled[0] - width) // 2
    top = (scaled[1] - height) // 2
    return img.crop((left, top, left + width, top + height))


def encode(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, subsampling=0)
    return buf.getvalue()


# --------------------------------------------------------------------------
# color transitions (slideshow.md obligation 6)
# --------------------------------------------------------------------------

def profile_path(image: str) -> Optional[str]:
    """common/images/x.jpg -> common/profiles/x.json (media-selection.md)."""
    parent, name = os.path.split(image)
    head, tail = os.path.split(parent)
    if tail != "images":
        return None
    return os.path.join(head, "profiles", os.path.splitext(name)[0] + ".json")


def hex_to_hue(value: str) -> Optional[float]:
    text = value.lstrip("#")
    if len(text) != 6:
        return None
    try:
        r, g, b = (int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return None
    high, low = max(r, g, b), min(r, g, b)
    span = high - low
    if span == 0:
        return 0.0
    if high == r:
        hue = 60.0 * (((g - b) / span) % 6.0)
    elif high == g:
        hue = 60.0 * (((b - r) / span) + 2.0)
    else:
        hue = 60.0 * (((r - g) / span) + 4.0)
    return hue


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


class ColorSender:
    """Validated, non-blocking zmq client.

    Every message is exactly three non-empty whitespace-free tokens: anything
    else corrupts the heap in FFmpeg 6.1.1's f_zmq.c and aborts the encoder.
    Sends run on a daemon thread with a depth-1 queue so a wedged endpoint can
    never stall frame production.
    """

    def __init__(self, endpoint: str, transition: float) -> None:
        self.endpoint = endpoint
        self.transition = max(0.0, transition)
        self.queue: "queue.Queue[list[str]]" = queue.Queue(maxsize=1)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    @staticmethod
    def valid(message: str) -> bool:
        tokens = message.split(" ")
        return len(tokens) == 3 and all(t and not t.isspace() for t in tokens)

    def _run(self) -> None:
        try:
            import zmq
        except ImportError:
            log("zmq_unavailable", endpoint=self.endpoint)
            return
        ctx = zmq.Context.instance()
        sock = None
        while True:
            batch = self.queue.get()
            for message in batch:
                if not self.valid(message):
                    log("zmq_rejected", message=repr(message))
                    continue
                try:
                    if sock is None:
                        sock = ctx.socket(zmq.REQ)
                        sock.setsockopt(zmq.LINGER, 0)
                        sock.setsockopt(zmq.RCVTIMEO, 2000)
                        sock.setsockopt(zmq.SNDTIMEO, 2000)
                        sock.connect(self.endpoint)
                    sock.send_string(message)
                    reply = sock.recv_string()
                    if reply.split(" ", 1)[0] != "0":
                        log("zmq_reply", message=repr(message), reply=repr(reply))
                except Exception as exc:  # REQ is stateful; a timeout poisons it
                    log("zmq_send_failed", error=type(exc).__name__)
                    if sock is not None:
                        sock.close(0)
                        sock = None

    def ramp(self, target: str, param: str, start: float, end: float,
             t0: float) -> str:
        """One command installing a self-animating expression — no per-frame traffic."""
        if self.transition <= 0.0:
            return f"{target} {param} {end:.4f}"
        expr = (f"({start:.4f}+({end - start:.4f})"
                f"*min(max((t-{t0:.3f})/{self.transition:.3f},0),1))")
        return f"{target} {param} {expr}"

    def apply(self, image: str, stream_time: float) -> None:
        path = profile_path(image)
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                profile = json.load(handle)
        except (OSError, ValueError) as exc:
            log("profile_unreadable", path=path, error=type(exc).__name__)
            return
        hue = hex_to_hue(str(profile.get("accent", "")))
        brightness = clamp(float(profile.get("brightness", 0.5)), 0.0, 1.0)
        warmth = clamp(float(profile.get("warmth", 0.0)), -1.0, 1.0)
        messages = [
            self.ramp("eq@eq", "brightness", 0.0,
                      clamp((brightness - 0.5) * 0.4, -1.0, 1.0), stream_time),
            self.ramp("eq@eq", "saturation", 1.0,
                      clamp(1.0 + 0.25 * warmth, 0.0, 3.0), stream_time),
        ]
        if hue is not None:
            messages.append(f"hue@hue h {clamp(hue, -360.0, 360.0):.2f}")
        try:
            self.queue.put_nowait(messages)
        except queue.Full:
            log("zmq_backlogged")


# --------------------------------------------------------------------------
# producer
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    images_list: str
    width: int
    height: int
    fps: float
    hold: float
    fade: float
    quality: int
    order: str
    stats_interval: float
    zmq_endpoint: str
    transition: float


class Producer:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.out = sys.stdout.buffer
        self.period = 1.0 / cfg.fps
        self.hold_frames = max(1, round(cfg.hold * cfg.fps))
        self.fade_frames = max(0, round(cfg.fade * cfg.fps))
        self.frame = 0
        self.late = 0
        self.slides = 0
        self.emitted_bytes = 0
        self.deadline = 0.0
        self.next_stat = cfg.stats_interval
        self.started = 0.0
        self.first_write: Optional[float] = None
        self.color = (ColorSender(cfg.zmq_endpoint, cfg.transition)
                       if cfg.zmq_endpoint else None)
        self.now = nowstate.start_writer()
        self.playlist: list[str] = []
        self.order: list[str] = []
        self.index = 0

    # -- pacing ------------------------------------------------------------
    def emit(self, payload: bytes) -> None:
        """Sleep only when ahead.

        Being late is not an error and frames are never dropped to catch up:
        the pipe is flow-controlled and FFmpeg's read rate is authoritative.
        During FFmpeg initialisation the producer is throttled to ~1.5 fps for
        4-6 s; that permanently offsets the slideshow timeline and is benign,
        so the deadline is re-based rather than clawed back.
        """
        slack = self.deadline - time.monotonic()
        if slack > 0:
            time.sleep(slack)
            self.deadline += self.period
        else:
            self.late += 1
            self.deadline = time.monotonic() + self.period
        self.out.write(payload)
        self.out.flush()
        if self.first_write is None:
            self.first_write = time.monotonic() - self.started
            log("first_frame_accepted", after_s=f"{self.first_write:.2f}",
                bytes=len(payload))
        self.emitted_bytes += len(payload)
        self.frame += 1
        self.maybe_stats()

    def stream_time(self) -> float:
        """PTS is derived from -framerate, so frame index is exact stream time."""
        return self.frame / self.cfg.fps

    def maybe_stats(self) -> None:
        elapsed = time.monotonic() - self.started
        if elapsed < self.next_stat:
            return
        self.next_stat += self.cfg.stats_interval
        log("stats", t=f"{elapsed:.1f}", frames=self.frame,
            avg_fps=f"{self.frame / elapsed:.2f}", late=self.late,
            slides=self.slides, slide_count=len(self.playlist),
            mbps=f"{self.emitted_bytes * 8 / elapsed / 1e6:.2f}")

    # -- slide selection ---------------------------------------------------
    def rescan(self) -> None:
        """Called only between slides — never mid-fade."""
        found = read_list(self.cfg.images_list)
        if not found:
            if self.playlist:
                log("list_empty_holding", count=len(self.playlist))
            return
        if found == self.playlist:
            return
        log("list_changed", was=len(self.playlist), now=len(found))
        self.playlist = found
        self.order = list(found)
        if self.cfg.order == "shuffle":
            random.shuffle(self.order)
        self.index = 0

    def advance(self, current: str) -> Optional[tuple[str, Image.Image, bytes]]:
        """Next loadable slide, or None when nothing in the list opens."""
        if not self.order:
            return None
        if current in self.order:
            self.index = (self.order.index(current) + 1) % len(self.order)
        for _ in range(len(self.order)):
            path = self.order[self.index]
            self.index = (self.index + 1) % len(self.order)
            if path == current and len(self.order) > 1:
                continue
            try:
                img = fit(path, self.cfg.width, self.cfg.height)
            except (OSError, ValueError) as exc:
                log("slide_unreadable", path=path, error=type(exc).__name__)
                continue
            return path, img, encode(img, self.cfg.quality)
        return None

    # -- main loop ---------------------------------------------------------
    def run(self) -> int:
        self.rescan()
        if not self.order:
            log("fatal", reason="no_slides", list=self.cfg.images_list)
            return 2
        first = self.advance("")
        if first is None:
            log("fatal", reason="no_loadable_slides", list=self.cfg.images_list)
            return 2
        cur_path, cur_img, cur_bytes = first
        if self.now is not None:
            self.now.set_slide(cur_path)
        log("start", list=self.cfg.images_list, slides=len(self.order),
            geometry=f"{self.cfg.width}x{self.cfg.height}", fps=self.cfg.fps,
            hold_frames=self.hold_frames, fade_frames=self.fade_frames,
            quality=self.cfg.quality, order=self.cfg.order)

        self.started = time.monotonic()
        self.deadline = self.started + self.period

        while True:
            for _ in range(self.hold_frames):
                self.emit(cur_bytes)

            self.rescan()
            nxt = self.advance(cur_path)
            if nxt is None:
                continue
            nxt_path, nxt_img, nxt_bytes = nxt
            self.slides += 1
            # Published as the crossfade starts, with the color ramp, because
            # that is when the viewer sees the new slide arrive.
            if self.now is not None:
                self.now.set_slide(nxt_path)
            if self.color is not None:
                self.color.apply(nxt_path, self.stream_time())

            for step in range(1, self.fade_frames + 1):
                alpha = step / (self.fade_frames + 1)
                self.emit(encode(Image.blend(cur_img, nxt_img, alpha),
                                 self.cfg.quality))

            cur_path, cur_img, cur_bytes = nxt_path, nxt_img, nxt_bytes


def parse_args() -> Settings:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--images-list", default=env_str("IMAGES_LIST",
                                                     "/media/channel/images.list"))
    ap.add_argument("--width", type=int, default=env_int("WIDTH", 1280))
    ap.add_argument("--height", type=int, default=env_int("HEIGHT", 720))
    ap.add_argument("--fps", type=float, default=env_float("PRODUCER_FPS", 10.0))
    ap.add_argument("--hold", type=float, default=env_float("HOLD_SECONDS", 20.0))
    ap.add_argument("--fade", type=float, default=env_float("FADE_SECONDS", 2.0))
    ap.add_argument("--quality", type=int, default=env_int("JPEG_QUALITY", 88))
    ap.add_argument("--order", choices=("sequential", "shuffle"),
                    default=env_str("SLIDESHOW_ORDER", "sequential"))
    ap.add_argument("--stats-interval", type=float,
                    default=env_float("STATS_INTERVAL", 30.0))
    ap.add_argument("--zmq-endpoint", default=env_str("ZMQ_ENDPOINT", ""))
    ap.add_argument("--transition", type=float,
                    default=env_float("COLOR_TRANSITION_SECONDS", 2.0))
    args = ap.parse_args()
    if args.fps <= 0:
        ap.error("--fps must be positive")
    if args.width <= 0 or args.height <= 0:
        ap.error("--width/--height must be positive")
    return Settings(
        images_list=args.images_list, width=args.width, height=args.height,
        fps=args.fps, hold=args.hold, fade=args.fade, quality=args.quality,
        order=args.order, stats_interval=args.stats_interval,
        zmq_endpoint=args.zmq_endpoint, transition=args.transition,
    )


def main() -> int:
    return Producer(parse_args()).run()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BrokenPipeError, KeyboardInterrupt):
        # The compositor shutting down normally.
        sys.exit(0)
