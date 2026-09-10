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

LIST_POLL_SECONDS = 1.0


def read_list(path: str) -> list[str]:
    """One absolute path per line. No timings — see media-selection.md.

    Opened fresh every time: the backend replaces this file with an atomic
    same-directory rename, so a retained handle would read the old inode
    forever. Lines are never split on whitespace — filenames contain spaces.
    Raises OSError; the caller owns the policy for a list it cannot read.
    """
    with open(path, "r", encoding="utf-8") as handle:
        raw = handle.read().splitlines()
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


def stat_key(path: str) -> Optional[tuple[int, int, int, int]]:
    """Cheap change token: st_ino catches the rename, st_ctime_ns catches chmod."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)


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


@dataclass(frozen=True)
class Slide:
    """Everything the write loop needs, resolved before it asks for it."""

    path: str
    image: Image.Image
    jpeg: bytes
    profile: Optional[dict]


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


def read_profile(image: str) -> Optional[dict]:
    """Read on the loader thread, applied later on the write loop."""
    path = profile_path(image)
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log("profile_unreadable", path=path, error=type(exc).__name__)
        return None


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


def viz_rotation(baked: str, wanted: str) -> Optional[float]:
    baked_hue = hex_to_hue(baked)
    wanted_hue = hex_to_hue(wanted)
    if baked_hue is None or wanted_hue is None:
        return None
    return round((wanted_hue - baked_hue + 180.0) % 360.0 - 180.0, 2)


def nearest_rotation(start: float, target: float) -> float:
    start = canonical_rotation(start)
    target = canonical_rotation(target)
    delta = (target - start + 180.0) % 360.0 - 180.0
    return start + delta


def canonical_rotation(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


COLOR_MODES = ("auto", "manual")


def resolve_color_mode(value: str) -> str:
    """In `manual` the operator owns color and the backend sends it directly;
    this producer must not overwrite it at the next slide.

    Anything unset or unrecognized is `auto`, the behavior every older compose
    file expects.
    """
    mode = value.strip().lower()
    if mode in COLOR_MODES:
        return mode
    log("color_mode_unknown", value=repr(value), fallback="auto")
    return "auto"


COLOR_MODE_POLL_SECONDS = 0.5


class ColorModeWatcher:
    """Watches ``<run_dir>/color-mode`` so an automatic/manual switch in the UI
    reaches a producer that is already running.

    The alternative is restarting the compositor, which costs a real gap and a
    new YouTube ingest session. The backend publishes the mode with a
    same-directory temp plus rename, so the file is never seen half-written but
    a retained handle would read the dead inode forever — hence a stat token and
    a fresh open only when it moves.

    Runs on its own thread: the write loop may do no filesystem work, and the
    loader thread's cadence is already perturbed by multi-hundred-ms decodes.
    """

    def __init__(self, path: str, initial: str) -> None:
        self.path = path
        self.mode = initial
        self.key: Optional[tuple[int, int, int, int]] = None
        self.warned = ""

    def start(self) -> None:
        if not self.path:
            log("color_mode_watch_off", mode=self.mode)
            return
        self.poll()
        threading.Thread(target=self._run, daemon=True, name="colormode").start()
        log("color_mode_watching", path=self.path, mode=self.mode,
            interval=COLOR_MODE_POLL_SECONDS)

    def _run(self) -> None:
        while True:
            time.sleep(COLOR_MODE_POLL_SECONDS)
            try:
                self.poll()
            except Exception as exc:  # a watcher fault must never stop frames
                self.warn(f"poll:{type(exc).__name__}", "color_mode_error",
                          error=type(exc).__name__)

    def poll(self) -> None:
        key = stat_key(self.path)
        if key is None or key == self.key:
            return  # absent or unmoved: whatever mode is in force stands
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = handle.read(64)
        except OSError as exc:
            # Key deliberately not adopted, so the next poll retries.
            self.warn(f"error:{type(exc).__name__}", "color_mode_unreadable",
                      error=type(exc).__name__)
            return
        self.key = key
        value = raw.strip().lower()
        if value not in COLOR_MODES:
            self.warn(f"value:{value}", "color_mode_ignored",
                      value=repr(raw[:32]), keeping=self.mode)
            return
        self.warned = ""
        self.mode = value  # a str rebind; the write loop reads it unlocked

    def warn(self, token: str, event: str, **fields: object) -> None:
        """A file stuck in one bad state logs once, not once per poll."""
        if token == self.warned:
            return
        self.warned = token
        log(event, path=self.path, **fields)


class ColorSender:
    """Validated, non-blocking zmq client.

    Every message is exactly three non-empty whitespace-free tokens: anything
    else corrupts the heap in FFmpeg 6.1.1's f_zmq.c and aborts the encoder.
    Sends run on a daemon thread with a depth-1 queue so a wedged endpoint can
    never stall frame production.
    """

    def __init__(self, endpoint: str, transition: float, enabled: bool,
                 baked_accent: str = "") -> None:
        self.endpoint = endpoint
        self.transition = max(0.0, transition)
        self.enabled = enabled
        self.baked_accent = baked_accent
        self.brightness = 0.0
        self.saturation = 1.0
        self.viz_hue = 0.0
        self.ramp_start = 0.0
        self.start_brightness = self.brightness
        self.start_saturation = self.saturation
        self.start_viz_hue = self.viz_hue
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
                # Re-checked here: a batch queued microseconds before a switch
                # to manual must not land on top of the operator's own color.
                if not self.enabled:
                    break
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

    def targets(self, profile: dict, current_hue: Optional[float] = None
                ) -> tuple[float, float, float]:
        rotation = viz_rotation(self.baked_accent, str(profile.get("accent", "")))
        brightness = clamp(float(profile.get("brightness", 0.5)), 0.0, 1.0)
        warmth = clamp(float(profile.get("warmth", 0.0)), -1.0, 1.0)
        target_brightness = clamp((brightness - 0.5) * 0.4, -1.0, 1.0)
        target_saturation = clamp(1.0 + 0.25 * warmth, 0.0, 3.0)
        hue_start = self.viz_hue if current_hue is None else current_hue
        target_hue = hue_start
        if rotation is not None:
            target_hue = nearest_rotation(hue_start, rotation)
        return target_brightness, target_saturation, target_hue

    def current(self, stream_time: float) -> tuple[float, float, float]:
        if self.transition <= 0.0:
            return self.brightness, self.saturation, self.viz_hue
        progress = clamp(
            (stream_time - self.ramp_start) / self.transition, 0.0, 1.0
        )
        return (
            self.start_brightness
            + (self.brightness - self.start_brightness) * progress,
            self.start_saturation
            + (self.saturation - self.start_saturation) * progress,
            canonical_rotation(
                self.start_viz_hue
                + (self.viz_hue - self.start_viz_hue) * progress
            ),
        )

    def adopt(self, profile: dict) -> None:
        """Track a backend-applied color without sending a duplicate command."""
        self.brightness, self.saturation, self.viz_hue = self.targets(profile)
        self.start_brightness = self.brightness
        self.start_saturation = self.saturation
        self.start_viz_hue = self.viz_hue
        self.ramp_start = 0.0

    def apply(self, profile: dict, stream_time: float) -> None:
        if not self.enabled:
            return
        current_brightness, current_saturation, current_hue = self.current(stream_time)
        target_brightness, target_saturation, target_hue = self.targets(
            profile, current_hue
        )
        messages = [
            self.ramp("eq@eq", "brightness", current_brightness,
                      target_brightness, stream_time),
            self.ramp("eq@eq", "saturation", current_saturation,
                      target_saturation, stream_time),
        ]
        if target_hue != current_hue:
            messages.append(
                self.ramp("hue@viz", "h", current_hue, target_hue, stream_time)
            )
        try:
            self.queue.put_nowait(messages)
        except queue.Full:
            log("zmq_backlogged")
            return
        self.start_brightness = current_brightness
        self.start_saturation = current_saturation
        self.start_viz_hue = current_hue
        self.brightness = target_brightness
        self.saturation = target_saturation
        self.viz_hue = target_hue
        self.ramp_start = stream_time


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
    color_mode: str
    color_mode_file: str
    # Where the slide on screen is recorded, so a restart can pick it up again.
    position_file: str = ""
    baked_accent: str = ""


def read_position(path: str) -> str:
    """The slide that was on screen when the last compositor stopped."""
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def write_position(path: str, slide: str) -> None:
    """Never fatal: losing the position costs continuity, not the broadcast."""
    if not path:
        return
    try:
        temporary = f"{path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(slide)
        os.replace(temporary, path)
    except OSError as exc:
        log("position_write_failed", error=type(exc).__name__)


class SlideLoader:
    """Owns images.list, slide order and image decode, off the write loop.

    Filesystem work between frames is a pause in a pipe FFmpeg is reading:
    decoding one 4000x3000 upload down to 720p was measured at 315-453 ms,
    four frame periods at 10 fps. This thread prepares the next slide during
    the current slide's hold, and re-reads the list only when a stat() shows
    it changed.
    """

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.playlist: list[str] = []
        self.order: list[str] = []
        self.index = 0
        self.key: Optional[tuple[int, int, int, int]] = None
        self.ready: Optional[Slide] = None
        self.current = ""
        self.holding = False
        # Consumed by the first adopt() only: after that the live position wins.
        self.resume = read_position(cfg.position_file)
        if self.resume:
            log("slide_resume", path=self.resume)

    def start(self) -> None:
        self.poll()
        self.prepare()
        threading.Thread(target=self._run, daemon=True, name="slides").start()

    def _run(self) -> None:
        while True:
            self.wake.wait(LIST_POLL_SECONDS)
            self.wake.clear()
            try:
                self.poll()
                self.prepare()
            except Exception as exc:  # a loader fault must never stop frames
                log("loader_error", error=type(exc).__name__)

    # -- list --------------------------------------------------------------
    def poll(self) -> None:
        key = stat_key(self.cfg.images_list)
        if key is not None and key == self.key:
            return
        try:
            found, reason = read_list(self.cfg.images_list), "empty"
        except OSError as exc:
            found, reason = [], type(exc).__name__
        if not found:
            if not self.holding:
                self.holding = True
                log("list_holding", path=self.cfg.images_list, reason=reason,
                    held=len(self.order))
            self.key = None  # chmod does not move mtime; re-read next poll
            return
        self.holding = False
        self.key = key
        if found != self.playlist:
            self.adopt(found)

    def adopt(self, found: list[str]) -> None:
        log("list_changed", was=len(self.playlist), now=len(found))
        order = list(found)
        if self.cfg.order == "shuffle":
            random.shuffle(order)
        self.playlist = found
        self.order = order
        with self.lock:
            current = self.current
            if self.ready is not None and self.ready.path not in found:
                self.ready = None  # dropped from the list, so it loses its turn
        self.index = order.index(current) + 1 if current in order else 0
        if not current and self.resume in order:
            # Where the last compositor left off. Not +1: that slide was on
            # screen when the graph was rebuilt, so it never finished its hold.
            self.index = order.index(self.resume)
        self.resume = ""
        self.index %= len(order)

    # -- slide selection ---------------------------------------------------
    def prepare(self) -> None:
        """Decode and encode the next slide before the write loop asks for it."""
        with self.lock:
            if self.ready is not None:
                return
            current = self.current
        order = self.order
        for _ in range(len(order)):
            path = order[self.index]
            self.index = (self.index + 1) % len(order)
            if path == current and len(order) > 1:
                continue
            try:
                img = fit(path, self.cfg.width, self.cfg.height)
            except (OSError, ValueError) as exc:
                log("slide_unreadable", path=path, error=type(exc).__name__)
                continue
            slide = Slide(path, img, encode(img, self.cfg.quality),
                          read_profile(path))
            with self.lock:
                self.ready = slide
            return

    def take(self) -> Optional[Slide]:
        """Never blocks. None means keep showing what is already on screen."""
        with self.lock:
            slide = self.ready
            if slide is None:
                return None
            self.ready = None
            self.current = slide.path
        self.wake.set()
        write_position(self.cfg.position_file, slide.path)
        return slide


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
        # The sender exists in either mode: the operator can switch to automatic
        # at any moment and a restart to build one is the thing to avoid. The
        # mode gates it instead.
        self.mode_watcher = ColorModeWatcher(cfg.color_mode_file, cfg.color_mode)
        self.mode_watcher.start()
        self.mode = self.mode_watcher.mode
        self.slide_profile: Optional[dict] = None
        self.color: Optional[ColorSender] = None
        if not cfg.zmq_endpoint:
            log("color_disabled", reason="no_endpoint")
        else:
            self.color = ColorSender(cfg.zmq_endpoint, cfg.transition,
                                     enabled=self.mode == "auto",
                                     baked_accent=cfg.baked_accent)
        self.now = nowstate.start_writer()
        self.loader = SlideLoader(cfg)

    def sync_color_mode(self) -> None:
        """One in-memory compare per frame; the watcher thread owns the file.

        Per frame rather than per slide because a slide can be 20 s away, and
        the operator expects the switch to land now.
        """
        mode = self.mode_watcher.mode
        if mode == self.mode:
            return
        self.mode = mode
        if self.color is not None:
            self.color.enabled = mode == "auto"
            # The backend fades the current manual color to this slide before
            # publishing auto mode. Adopt that target without racing it with a
            # second command; the next slide then starts from the right values.
            if mode == "auto" and self.slide_profile is not None:
                self.color.adopt(self.slide_profile)
        log("color_mode_changed", mode=mode, frame=self.frame)

    # -- pacing ------------------------------------------------------------
    def emit(self, payload: bytes) -> None:
        """Sleep only when ahead.

        Being late is not an error and frames are never dropped to catch up:
        the pipe is flow-controlled and FFmpeg's read rate is authoritative.
        During FFmpeg initialisation the producer is throttled to ~1.5 fps for
        4-6 s; that permanently offsets the slideshow timeline and is benign,
        so the deadline is re-based rather than clawed back.
        """
        self.sync_color_mode()
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
            slides=self.slides, slide_count=len(self.loader.playlist),
            mbps=f"{self.emitted_bytes * 8 / elapsed / 1e6:.2f}")

    # -- main loop ---------------------------------------------------------
    def run(self) -> int:
        self.loader.start()
        if not self.loader.order:
            log("fatal", reason="no_slides", list=self.cfg.images_list)
            return 2
        cur = self.loader.take()
        if cur is None:
            log("fatal", reason="no_loadable_slides", list=self.cfg.images_list)
            return 2
        if self.now is not None:
            self.now.set_slide(cur.path)
        self.slide_profile = cur.profile
        log("start", list=self.cfg.images_list, slides=len(self.loader.order),
            geometry=f"{self.cfg.width}x{self.cfg.height}", fps=self.cfg.fps,
            hold_frames=self.hold_frames, fade_frames=self.fade_frames,
            quality=self.cfg.quality, order=self.cfg.order,
            color_mode=self.mode,
            color_mode_file=self.cfg.color_mode_file or "-",
            color_sender="on" if self.color is not None else "off")

        self.started = time.monotonic()
        self.deadline = self.started + self.period

        while True:
            for _ in range(self.hold_frames):
                self.emit(cur.jpeg)

            nxt = self.loader.take()
            while nxt is None:
                # Empty list, nothing loadable, or the next slide still
                # decoding. Hold the frame: black or a stalled pipe would end
                # the broadcast, a longer hold is invisible.
                self.emit(cur.jpeg)
                nxt = self.loader.take()

            self.slides += 1
            # Published as the crossfade starts, with the color ramp, because
            # that is when the viewer sees the new slide arrive.
            if self.now is not None:
                self.now.set_slide(nxt.path)
            self.slide_profile = nxt.profile
            if self.color is not None and nxt.profile is not None:
                self.color.apply(nxt.profile, self.stream_time())

            for step in range(1, self.fade_frames + 1):
                alpha = step / (self.fade_frames + 1)
                self.emit(encode(Image.blend(cur.image, nxt.image, alpha),
                                 self.cfg.quality))

            cur = nxt


def default_color_mode_file() -> str:
    """The run directory the backend publishes into, per on-disk.md.

    Empty when neither is set — outside a container there is nothing to watch,
    and the flag alone then behaves exactly as it did before.
    """
    return _run_dir_file("color-mode")


def default_position_file() -> str:
    return _run_dir_file("slide-position")


def _run_dir_file(leaf: str) -> str:
    run_dir = os.environ.get("RUN_DIR", "").strip()
    if not run_dir:
        channel = os.environ.get("CHANNEL_NAME", "").strip()
        if not channel:
            return ""
        run_dir = f"/run/ambient/{channel}"
    return os.path.join(run_dir, leaf)


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
    # No choices=: argparse would exit(2) on an unknown mode, and a producer
    # that refuses to start EOFs the pipe and ends the broadcast.
    ap.add_argument("--color-mode", default=env_str("COLOR_MODE", "auto"))
    ap.add_argument("--color-mode-file",
                    default=env_str("COLOR_MODE_FILE", default_color_mode_file()))
    ap.add_argument("--transition", type=float,
                    default=env_float("COLOR_TRANSITION_SECONDS", 2.0))
    ap.add_argument("--baked-accent", default=env_str("ACCENT", "#4FC3F7"))
    ap.add_argument("--position-file",
                    default=env_str("SLIDE_POSITION_FILE", default_position_file()))
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
        color_mode=resolve_color_mode(args.color_mode),
        color_mode_file=args.color_mode_file,
        position_file=args.position_file, baked_accent=args.baked_accent,
    )


def main() -> int:
    return Producer(parse_args()).run()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BrokenPipeError, KeyboardInterrupt):
        # The compositor shutting down normally.
        sys.exit(0)
