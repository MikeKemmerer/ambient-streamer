from __future__ import annotations

import importlib.util
import json
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("framekeeper", HERE / "framekeeper.py")
assert SPEC and SPEC.loader
FRAMEKEEPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FRAMEKEEPER)


class FramekeeperUnitTests(unittest.TestCase):
    def test_black_frame_is_limited_range_yuv420p(self) -> None:
        self.assertEqual(
            FRAMEKEEPER.black_frame(4, 2),
            bytes([16]) * 8 + bytes([128]) * 4,
        )

    def test_geometry_caps_are_enforced(self) -> None:
        FRAMEKEEPER.validate_geometry(1280, 720, 30)
        for geometry in ((1282, 720, 30), (1280, 722, 30), (1280, 720, 31), (3, 2, 30)):
            with self.subTest(geometry=geometry):
                with self.assertRaises(ValueError):
                    FRAMEKEEPER.validate_geometry(*geometry)


class FramekeeperProcessTests(unittest.TestCase):
    def test_status_write_failure_does_not_interrupt_frames(self) -> None:
        width, height, fps = 8, 4, 20
        frame_size = width * height * 3 // 2
        fallback = FRAMEKEEPER.black_frame(width, height)

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            socket_path = run_dir / "visualization.sock"
            status_path = run_dir / "visualization-status.json"
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(HERE / "framekeeper.py"),
                    "--input",
                    str(socket_path),
                    "--width",
                    str(width),
                    "--height",
                    str(height),
                    "--fps",
                    str(fps),
                    "--status",
                    str(status_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert process.stdout and process.stderr
            try:
                deadline = time.monotonic() + 3
                while not status_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(status_path.is_file())

                time.sleep(0.1)
                status_path.unlink()
                status_path.mkdir()
                frames = [process.stdout.read(frame_size) for _ in range(fps + 5)]
                self.assertEqual(frames, [fallback] * len(frames))
                self.assertIsNone(process.poll(), "status failure stopped frame output")
            finally:
                process.terminate()
                process.wait(timeout=3)

            error_output = process.stderr.read().decode()
            process.stdout.close()
            process.stderr.close()
            self.assertEqual(process.returncode, 0, error_output)
            self.assertIn('"event": "status-write-failed"', error_output)

    def test_writer_disconnect_returns_to_fallback_without_stopping(self) -> None:
        width, height, fps = 8, 4, 20
        frame_size = width * height * 3 // 2
        fallback = FRAMEKEEPER.black_frame(width, height)
        visual = bytes([200]) * (width * height) + bytes([128]) * (width * height // 2)

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            socket_path = run_dir / "visualization.sock"
            status_path = run_dir / "visualization-status.json"
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(HERE / "framekeeper.py"),
                    "--input",
                    str(socket_path),
                    "--width",
                    str(width),
                    "--height",
                    str(height),
                    "--fps",
                    str(fps),
                    "--stale-seconds",
                    "0.12",
                    "--status",
                    str(status_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert process.stdout and process.stderr
            try:
                deadline = time.monotonic() + 3
                while not socket_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(socket_path.exists())
                self.assertEqual(stat.S_IMODE(socket_path.stat().st_mode), 0o600)

                self.assertEqual(process.stdout.read(frame_size), fallback)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as writer:
                    writer.connect(str(socket_path))
                    writer.sendall(visual * 4)
                    frames = [process.stdout.read(frame_size) for _ in range(8)]
                self.assertIn(visual, frames)

                time.sleep(0.2)
                frames = [process.stdout.read(frame_size) for _ in range(5)]
                self.assertIn(fallback, frames)
                running = json.loads(status_path.read_text(encoding="utf-8"))
                self.assertTrue(running["ready"])
                self.assertGreater(running["received"], 0)
                self.assertEqual(running["reconnects"], 1)
            finally:
                process.terminate()
                process.wait(timeout=3)

            stopped = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertFalse(stopped["ready"])
            self.assertFalse(socket_path.exists())
            error_output = process.stderr.read().decode()
            process.stdout.close()
            process.stderr.close()
            self.assertEqual(process.returncode, 0, error_output)


if __name__ == "__main__":
    unittest.main()