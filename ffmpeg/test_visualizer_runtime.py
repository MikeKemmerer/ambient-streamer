from __future__ import annotations

import functools
import http.server
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def wait_for(predicate: Callable[[], bool], timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        pass


def stop_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
class VisualizerRuntimeTests(unittest.TestCase):
    def test_http_reconnect_options_are_not_applied_to_rtmp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            run_root = work / "run"
            run_dir = run_root / "test"
            run_dir.mkdir(parents=True)
            plugin_dir = work / "plugins" / "test-plugin"
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "config.json").write_text(
                json.dumps(
                    {
                        "name": "test-plugin",
                        "version": "test",
                        "requires_filters": [],
                        "parameters": {},
                    }
                ),
                encoding="utf-8",
            )
            (plugin_dir / "viz.ffmpeg").write_text(
                "[0:a]showwaves=s=${WIDTH}x${HEIGHT}:r=${FPS}[${OUT}]\n",
                encoding="utf-8",
            )
            arguments = work / "arguments.json"
            fake_ffmpeg = work / "ffmpeg"
            fake_ffmpeg.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "if '-filters' not in sys.argv:\n"
                "    open(os.environ['FFMPEG_ARGUMENTS'], 'w').write(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            fake_ffmpeg.chmod(0o700)
            socket_path = run_dir / "visualization.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            listener.listen(1)
            environment = {
                **os.environ,
                "CHANNEL_NAME": "test",
                "PLUGIN_NAME": "test-plugin",
                "WIDTH": "320",
                "HEIGHT": "180",
                "FPS": "30",
                "RUN_ROOT": str(run_root),
                "RUN_DIR": str(run_dir),
                "PLUGIN_DIR": str(work / "plugins"),
                "FFMPEG_BIN": str(fake_ffmpeg),
                "FFMPEG_ARGUMENTS": str(arguments),
                "PYTHON_BIN": sys.executable,
                "PLUGIN_PARAMS_BIN": str(HERE / "plugin_params.py"),
            }
            try:
                for audio_url, expects_reconnect in (
                    ("rtmp://relay/channel/preview", False),
                    ("http://icecast:8081/channel", True),
                ):
                    arguments.unlink(missing_ok=True)
                    subprocess.run(
                        ["bash", str(HERE / "visualizer-entrypoint.sh")],
                        env={**environment, "AUDIO_URL": audio_url},
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                    )
                    argv = json.loads(arguments.read_text(encoding="utf-8"))
                    self.assertEqual("-reconnect" in argv, expects_reconnect)
                    self.assertEqual(argv[argv.index("-i") + 1], audio_url)
            finally:
                listener.close()

    def test_entrypoint_escalates_unresponsive_ffmpeg_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            run_root = work / "run"
            run_dir = run_root / "test"
            run_dir.mkdir(parents=True)
            plugin_dir = work / "plugins" / "test-plugin"
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "config.json").write_text(
                json.dumps(
                    {
                        "name": "test-plugin",
                        "version": "test",
                        "requires_filters": [],
                        "parameters": {},
                    }
                ),
                encoding="utf-8",
            )
            (plugin_dir / "viz.ffmpeg").write_text(
                "[0:a]showwaves=s=${WIDTH}x${HEIGHT}:r=${FPS}[${OUT}]\n",
                encoding="utf-8",
            )
            child_pid_path = work / "ffmpeg.pid"
            fake_ffmpeg = work / "ffmpeg"
            fake_ffmpeg.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "if [[ \" $* \" == *\" -filters \"* ]]; then exit 0; fi\n"
                "exec \"$PYTHON_BIN\" -c 'import os,signal,time; "
                "from pathlib import Path; "
                "signal.signal(signal.SIGINT, signal.SIG_IGN); "
                "Path(os.environ[\"FAKE_FFMPEG_PID_FILE\"]).write_text(str(os.getpid())); "
                "time.sleep(60)'\n",
                encoding="utf-8",
            )
            fake_ffmpeg.chmod(0o700)
            socket_path = run_dir / "visualization.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            listener.listen(1)
            environment = {
                **os.environ,
                "CHANNEL_NAME": "test",
                "PLUGIN_NAME": "test-plugin",
                "WIDTH": "320",
                "HEIGHT": "180",
                "FPS": "30",
                "RUN_ROOT": str(run_root),
                "RUN_DIR": str(run_dir),
                "PLUGIN_DIR": str(work / "plugins"),
                "FFMPEG_BIN": str(fake_ffmpeg),
                "FAKE_FFMPEG_PID_FILE": str(child_pid_path),
                "PYTHON_BIN": sys.executable,
                "PLUGIN_PARAMS_BIN": str(HERE / "plugin_params.py"),
            }
            process = subprocess.Popen(
                ["bash", str(HERE / "visualizer-entrypoint.sh")],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            assert process.stderr
            child_pid: int | None = None
            try:
                self.assertTrue(wait_for(child_pid_path.exists), "fake ffmpeg did not start")
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                started = time.monotonic()
                process.terminate()
                process.wait(timeout=5)
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 4)
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                listener.close()

            logs = process.stderr.read().decode()
            process.stderr.close()
            self.assertIn("ffmpeg did not stop after SIGINT; sending SIGKILL", logs)

    def test_composer_cleanup_only_unlinks_owned_fifos(self) -> None:
        source = (HERE / "entrypoint.sh").read_text(encoding="utf-8")

        self.assertIn('SLIDES_FIFO_ID="$(path_identity "$SLIDES_FIFO")"', source)
        self.assertIn('VIZ_FIFO_ID="$(path_identity "$VIZ_FIFO")"', source)
        self.assertIn('unlink_owned "$SLIDES_FIFO" "$SLIDES_FIFO_ID"', source)
        self.assertIn('unlink_owned "$VIZ_FIFO" "$VIZ_FIFO_ID"', source)
        self.assertNotIn('[[ -S "$VIZ_SOCKET" ]] && unlink', source)

    def test_thickness_dilation_expands_a_stroke(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)

            def render(thickness: int) -> bytes:
                output = work / f"stroke-{thickness}.gray"
                filters = (
                    "format=gbrp,"
                    f"dilation=coordinates=255:enable='gte({thickness},2)',"
                    f"dilation=coordinates=255:enable='gte({thickness},3)',"
                    f"dilation=coordinates=255:enable='gte({thickness},4)',"
                    "format=gray"
                )
                subprocess.run(
                    [
                        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "color=black:s=32x32:d=1",
                        "-vf", f"drawbox=x=16:y=16:w=1:h=1:color=white:t=fill,{filters}",
                        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", str(output),
                    ],
                    check=True,
                )
                return output.read_bytes()

            thin = render(1)
            thick = render(4)
            self.assertGreater(sum(value > 32 for value in thick), sum(value > 32 for value in thin))

    def test_all_real_plugins_write_frames_to_framekeeper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            run_root = work / "run"
            run_dir = run_root / "test"
            run_dir.mkdir(parents=True)
            audio = work / "audio.wav"
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=44100:duration=2",
                    "-ac",
                    "2",
                    str(audio),
                ],
                check=True,
            )

            handler = functools.partial(
                QuietHandler,
                directory=str(work),
            )
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()

            socket_path = run_dir / "visualization.sock"
            status_path = run_dir / "visualization-status.json"
            progress_path = run_dir / "main.progress"
            keeper = subprocess.Popen(
                [
                    sys.executable,
                    str(HERE / "framekeeper.py"),
                    "--input",
                    str(socket_path),
                    "--width",
                    "1280",
                    "--height",
                    "720",
                    "--fps",
                    "30",
                    "--status",
                    str(status_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert keeper.stdout and keeper.stderr
            graph = (
                "[1:v]scale=1280:720:flags=fast_bilinear,fps=30,realtime,"
                "split=2[vizc][vizm];"
                "[vizm]format=gray,lut=y='(val-16)*1.16438356*0.65'[alpha];"
                "[vizc][alpha]alphamerge[vizrgba];"
                "[0:v][vizrgba]overlay=eof_action=pass:format=auto,"
                "format=yuv420p[out]"
            )
            compositor = subprocess.Popen(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-progress",
                    str(progress_path),
                    "-re",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=0x202040:s=1280x720:r=30",
                    "-f",
                    "rawvideo",
                    "-pixel_format",
                    "yuv420p",
                    "-video_size",
                    "1280x720",
                    "-framerate",
                    "30",
                    "-i",
                    "pipe:0",
                    "-filter_complex",
                    graph,
                    "-map",
                    "[out]",
                    "-f",
                    "null",
                    "-",
                ],
                stdin=keeper.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            assert compositor.stderr
            keeper.stdout.close()
            compositor_pid = compositor.pid
            visualizer: subprocess.Popen[bytes] | None = None
            try:
                self.assertTrue(wait_for(socket_path.exists), "framekeeper socket was not created")
                plugins = sorted(path.parent.name for path in (ROOT / "plugins").glob("*/viz.ffmpeg"))
                received = 0
                for reconnects, plugin in enumerate(plugins, start=1):
                    with self.subTest(plugin=plugin):
                        audio_url = f"http://127.0.0.1:{server.server_port}/audio.wav"
                        environment = {
                            **os.environ,
                            "CHANNEL_NAME": "test",
                            "PLUGIN_NAME": plugin,
                            "WIDTH": "1920",
                            "HEIGHT": "1080",
                            "FPS": "30",
                            "RUN_ROOT": str(run_root),
                            "RUN_DIR": str(run_dir),
                            "PLUGIN_DIR": str(ROOT / "plugins"),
                            "AUDIO_URL": audio_url,
                            "SOURCE_REVISION": "test-revision",
                            "FFMPEG_LOGLEVEL": "error",
                        }
                        if plugin in {
                            "showwaves-classic",
                            "minimal-line",
                            "avectorscope-lissajous",
                        }:
                            environment["PLUGIN_PARAMS"] = json.dumps(
                                {plugin: {"thickness": 4}}
                            )
                        visualizer = subprocess.Popen(
                            ["bash", str(HERE / "visualizer-entrypoint.sh")],
                            env=environment,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE,
                            start_new_session=True,
                        )
                        assert visualizer.stderr

                        def frames_arrived() -> bool:
                            try:
                                status = json.loads(status_path.read_text(encoding="utf-8"))
                            except (FileNotFoundError, json.JSONDecodeError):
                                return False
                            return status["received"] > received and status["reconnects"] == reconnects

                        self.assertTrue(wait_for(frames_arrived), f"{plugin} delivered no frames")
                        returncode = visualizer.poll()
                        stop_group(visualizer)
                        logs = visualizer.stderr.read().decode()
                        visualizer.stderr.close()
                        if returncode is not None:
                            self.assertEqual(returncode, 0, logs)
                        self.assertIn("source=ffmpeg/visualizer-entrypoint.sh", logs)
                        self.assertIn("revision=test-revision", logs)
                        self.assertIn("layer=1280x720@30", logs)
                        self.assertNotIn(audio_url, logs)
                        received = json.loads(status_path.read_text(encoding="utf-8"))["received"]
                        self.assertEqual(compositor.pid, compositor_pid)
                        self.assertIsNone(compositor.poll(), "stable compositor exited during replacement")
                        visualizer = None

                pacing_speed: float | None = None

                def compositor_pacing_is_plausible() -> bool:
                    nonlocal pacing_speed
                    try:
                        current = dict(
                            line.split("=", 1)
                            for line in progress_path.read_text(encoding="utf-8").splitlines()
                            if "=" in line
                        )
                        pacing_speed = float(current["speed"].removesuffix("x"))
                    except (FileNotFoundError, KeyError, ValueError):
                        return False
                    return 0.9 <= pacing_speed <= 1.1

                self.assertTrue(
                    wait_for(compositor_pacing_is_plausible, timeout=5),
                    f"compositor speed did not stabilize near realtime: {pacing_speed}",
                )
            finally:
                if visualizer is not None:
                    stop_group(visualizer)
                    if visualizer.stderr:
                        visualizer.stderr.close()
                stop_group(compositor)
                compositor.stderr.close()
                if keeper.poll() is None:
                    keeper.terminate()
                keeper.wait(timeout=3)
                keeper.stderr.close()
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=3)

            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertGreater(status["received"], 0)
            self.assertEqual(status["reconnects"], len(plugins))
            self.assertFalse(status["ready"])
            progress = progress_path.read_text(encoding="utf-8")
            self.assertIn("progress=continue", progress)
            latest = dict(
                line.split("=", 1)
                for line in progress.splitlines()
                if "=" in line
            )
            self.assertGreater(int(latest["frame"]), 0)
            self.assertEqual(latest["drop_frames"], "0")
            self.assertEqual(latest["dup_frames"], "0")


if __name__ == "__main__":
    unittest.main()