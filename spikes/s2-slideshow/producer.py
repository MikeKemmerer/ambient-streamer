#!/usr/bin/env python3
"""Slideshow frame producer for FFmpeg ``-f image2pipe``.

Owns slide order, hold time and crossfade so the image set can change while the
composer keeps running. Writes encoded JPEG/PNG frames to stdout, paced against
a monotonic clock at the declared producer frame rate.

Hold frames are encoded once and re-emitted byte-for-byte; only crossfade frames
cost real CPU.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
from typing import Optional

from PIL import Image

EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def scan(directory: str) -> list[str]:
    try:
        names = sorted(
            e.name for e in os.scandir(directory)
            if e.is_file() and os.path.splitext(e.name)[1].lower() in EXTS
        )
    except FileNotFoundError:
        return []
    return [os.path.join(directory, n) for n in names]


def fit(path: str, w: int, h: int) -> Image.Image:
    """Cover-fit to exactly WxH so every frame the pipe carries is identical size."""
    img = Image.open(path)
    img = img.convert("RGB")
    src_w, src_h = img.size
    scale = max(w / src_w, h / src_h)
    new = (max(w, int(src_w * scale + 0.5)), max(h, int(src_h * scale + 0.5)))
    img = img.resize(new, Image.BILINEAR)
    left = (new[0] - w) // 2
    top = (new[1] - h) // 2
    return img.crop((left, top, left + w, top + h))


class Encoder:
    def __init__(self, fmt: str, quality: int) -> None:
        self.fmt = fmt
        self.quality = quality

    def __call__(self, img: Image.Image) -> bytes:
        buf = io.BytesIO()
        if self.fmt == "jpeg":
            img.save(buf, format="JPEG", quality=self.quality, subsampling=0)
        elif self.fmt == "png":
            img.save(buf, format="PNG", compress_level=1)
        else:
            raise ValueError(self.fmt)
        return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=float, default=5.0, help="producer frame rate")
    ap.add_argument("--hold", type=float, default=4.0, help="seconds per slide")
    ap.add_argument("--fade", type=float, default=1.5, help="crossfade seconds")
    ap.add_argument("--format", choices=("jpeg", "png"), default="jpeg")
    ap.add_argument("--quality", type=int, default=88)
    ap.add_argument("--stats-interval", type=float, default=5.0)
    ap.add_argument("--stall-at", type=float, default=0.0,
                    help="debug: stop writing after N seconds")
    ap.add_argument("--exit-at", type=float, default=0.0,
                    help="debug: exit cleanly after N seconds")
    args = ap.parse_args()

    encode = Encoder(args.format, args.quality)
    out = sys.stdout.buffer
    period = 1.0 / args.fps
    fade_frames = max(0, int(round(args.fade * args.fps)))
    hold_frames = max(1, int(round(args.hold * args.fps)))

    playlist = scan(args.dir)
    if not playlist:
        print("producer: no images found", file=sys.stderr)
        return 2

    index = 0
    cur_path = playlist[0]
    cur_img = fit(cur_path, args.width, args.height)
    cur_bytes = encode(cur_img)

    frame = 0
    encoded_frames = 0
    encoded_bytes = 0
    late = 0
    slides = 0
    first_write: Optional[float] = None
    t0 = time.monotonic()
    last_t, last_frame = 0.0, 0
    next_stat = args.stats_interval

    def emit(payload: bytes) -> None:
        nonlocal frame, late, encoded_bytes, first_write
        target = t0 + frame * period
        delay = target - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        elif delay < -period:
            late += 1
        out.write(payload)
        out.flush()
        if first_write is None:
            first_write = time.monotonic() - t0
            print(f"producer: first frame accepted by reader at +{first_write:.2f}s "
                  f"({len(payload)/1024:.0f} KiB/frame)", file=sys.stderr, flush=True)
        encoded_bytes += len(payload)
        frame += 1

    def maybe_stats() -> None:
        nonlocal next_stat, last_t, last_frame
        el = time.monotonic() - t0
        if el < next_stat:
            return
        next_stat += args.stats_interval
        interval = (frame - last_frame) / max(1e-6, el - last_t)
        last_t, last_frame = el, frame
        print(
            f"producer stats t={el:.1f}s frames={frame} interval_fps={interval:.2f} "
            f"avg_fps={frame/el:.2f} encoded={encoded_frames} late={late} "
            f"slides={slides} mbps={encoded_bytes*8/el/1e6:.2f} playlist={len(playlist)}",
            file=sys.stderr, flush=True,
        )

    def debug_hooks() -> Optional[int]:
        el = time.monotonic() - t0
        if args.exit_at and el >= args.exit_at:
            print(f"producer: exit-at {el:.1f}s", file=sys.stderr, flush=True)
            return 0
        if args.stall_at and el >= args.stall_at:
            print(f"producer: stalling at {el:.1f}s", file=sys.stderr, flush=True)
            while True:
                time.sleep(3600)
        return None

    while True:
        for _ in range(hold_frames):
            rc = debug_hooks()
            if rc is not None:
                return rc
            emit(cur_bytes)
            maybe_stats()

        # Rescan between slides: additions/removals apply with no ffmpeg restart.
        found = scan(args.dir)
        if found:
            playlist = found
        index = (playlist.index(cur_path) + 1) % len(playlist) if cur_path in playlist \
            else index % len(playlist)
        nxt_path = playlist[index]
        nxt_img = fit(nxt_path, args.width, args.height)
        nxt_bytes = encode(nxt_img)
        slides += 1

        for i in range(1, fade_frames + 1):
            rc = debug_hooks()
            if rc is not None:
                return rc
            alpha = i / (fade_frames + 1)
            emit(encode(Image.blend(cur_img, nxt_img, alpha)))
            encoded_frames += 1
            maybe_stats()

        cur_path, cur_img, cur_bytes = nxt_path, nxt_img, nxt_bytes


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BrokenPipeError, KeyboardInterrupt):
        sys.exit(0)
