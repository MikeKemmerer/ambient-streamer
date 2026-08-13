"""Channel lifecycle: render the Compose file, then drive `docker compose`.

`docker/compose.channel.yml.j2` is owned by the infra lane and is read, never
written.

Two things about the invocation are load-bearing:

* **Both env files are passed, root first.** Compose resolves the implicit
  `.env` relative to the compose file's directory, so a file in
  `channels/<name>/` never sees the root `.env` and every
  `${ICECAST_SOURCE_PASSWORD}` in the template would resolve to empty.
* **Restart is make-before-break.** The replacement composer starts under a
  second project with its own container name, claims the relay path (MediaMTX
  `overridePublisher`), and only then is the outgoing one removed. Measured:
  kill-then-restart costs 5.14 s on the YouTube leg, takeover 1.03 s.

Everything is passed as argv; nothing here reaches a shell. A channel name from
a request is validated by `config.channel_directory` before it gets this far.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Iterator, Sequence

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError

from .config import ConfigError, ResolvedChannel, Workspace, channel_directory
from .events import CHANNEL_STATUS, EventHub
from .ffmpeg_cmd import ProbeResult, docker_probe_argv, probe_failure_detail
from .media import atomic_write_lines
from .models import ChannelState
from .zmqctl import SUCCESS, ZmqCommandError, validate_address, validate_message

LOG = logging.getLogger("ambient.supervisor")

TEMPLATE_DIR = "docker"
TEMPLATE_NAME = "compose.channel.yml.j2"

COMPOSER_SUFFIX = "-composer"
LIQUIDSOAP_SUFFIX = "-liquidsoap"
# The replacement slot used by a make-before-break restart.
NEXT_SUFFIX = "-next"

# Which container's stdout carries each service. The producer and the nowstate
# writer are threads inside the composer, so they share its stream.
LOG_SERVICE_CONTAINERS: dict[str, str] = {
    "compositor": "composer",
    "producer": "composer",
    "liquidsoap": "liquidsoap",
    "watchdog": "",
}
LOG_SERVICES = frozenset(LOG_SERVICE_CONTAINERS)

DEFAULT_TIMEOUT = 180.0
TAKEOVER_TIMEOUT = 60.0
PROBE_TIMEOUT = 120.0
RUN_DIR = "/run/ambient"
# Matches docker/compose.channel.yml.j2, which pins ambient-composer:dev.
DEFAULT_COMPOSER_IMAGE = "ambient-composer:dev"
# Matches ZMQ_BIND_HOST/ZMQ_BIND_PORT in docker/compose.channel.yml.j2.
ZMQ_ENDPOINT = "tcp://127.0.0.1:5555"

# Runs inside the composer, which ships python3-zmq. Both arguments arrive as
# argv, never through a shell.
_ZMQ_SENDER = (
    "import sys,zmq\n"
    "s=zmq.Context().socket(zmq.REQ)\n"
    "s.setsockopt(zmq.RCVTIMEO,5000)\n"
    "s.setsockopt(zmq.SNDTIMEO,5000)\n"
    "s.setsockopt(zmq.LINGER,0)\n"
    "s.connect(sys.argv[1])\n"
    "s.send_string(sys.argv[2])\n"
    "print(s.recv_string(),end='')\n"
)


class ComposeError(ValueError):
    """The Compose file could not be rendered, or did not render to valid YAML."""


class SupervisorError(RuntimeError):
    """A `docker` invocation failed."""


class ChannelBusy(RuntimeError):
    """The channel is in a state that forbids the requested operation."""


def compose_template_path(workspace: Workspace) -> Path:
    return workspace.root / TEMPLATE_DIR / TEMPLATE_NAME


def compose_context(workspace: Workspace, channel: ResolvedChannel) -> dict[str, str]:
    """The variables the template declares. `image_tag` is left to its default."""
    color = channel.config.color
    initial = channel.color_initial
    return {
        "channel": channel.name,
        "repo_root": str(workspace.root),
        "common_dir": str(workspace.common_dir),
        "channel_dir": str(channel.directory),
        # Per-channel, because both containers mount it at /var/log/ambient.
        "log_dir": str(workspace.log_dir / channel.name),
        "encoder": channel.encoder.value,
        # channel.liq reads CROSSFADE_SECONDS; without this the value resolved
        # from config.yaml never reaches it and the script default silently wins.
        "crossfade_seconds": str(channel.crossfade_seconds),
        # The entrypoint defaults HOT_SET to a single plugin, so without these the
        # channel's whole visualization selection is silently ignored.
        "active_plugin": channel.active_plugin,
        "hot_set": ",".join(channel.hot_set),
        "visualization": "on" if channel.visualization_enabled else "off",
        "run_dir": str(workspace.run_dir),
        # The entrypoint reads these from the environment; without them the
        # producer silently runs its own defaults and the channel's slideshow
        # timing is inert.
        "hold_seconds": f"{channel.hold_seconds:g}",
        "fade_seconds": f"{channel.fade_seconds:g}",
        "producer_fps": str(channel.producer_fps),
        "jpeg_quality": str(channel.jpeg_quality),
        # The entrypoint sizes everything from WIDTH/HEIGHT; it never reads a
        # "720p" style name, so passing only the name pins every channel to the
        # 1280x720 default and the resolution setting does nothing.
        "width": str(channel.width),
        "height": str(channel.height),
        "fps": str(channel.fps),
        # Without the mode the producer cannot tell a manual color from a
        # derived one and overwrites an operator's color at the next slide.
        "color_mode": channel.color_mode,
        "color_accent": color.manual.accent,
        "color_tint": color.manual.tint,
        "color_transition_seconds": f"{color.transition_seconds:g}",
        # The filtergraph is fixed at launch, so a manual color has to start
        # baked into eq/hue or a composer restart resets it to neutral.
        "color_init_hue": f"{initial.hue_degrees:g}",
        "color_init_saturation": f"{initial.saturation:g}",
        "color_init_brightness": f"{initial.brightness:g}",
    }


def render_compose(workspace: Workspace, channel: ResolvedChannel) -> str:
    """Render the template and prove the result is a usable Compose document."""
    template_path = compose_template_path(workspace)
    if not template_path.is_file():
        raise ComposeError(f"{template_path} is missing; cannot render a Compose file")

    # No autoescape: this is YAML, and HTML escaping would corrupt host paths.
    environment = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        undefined=StrictUndefined,
        autoescape=False,
    )
    try:
        rendered = environment.get_template(TEMPLATE_NAME).render(
            compose_context(workspace, channel)
        )
    except TemplateError as exc:
        raise ComposeError(f"{template_path}: {exc}") from exc

    # A template typo must fail here, not later as an opaque `docker compose` error.
    try:
        document = yaml.safe_load(rendered)
    except yaml.YAMLError as exc:
        raise ComposeError(
            f"{template_path} rendered invalid YAML for channel {channel.name!r}: {exc}"
        ) from exc
    if not isinstance(document, dict) or not document.get("services"):
        raise ComposeError(
            f"{template_path} rendered no services for channel {channel.name!r}"
        )
    return rendered


def write_compose(workspace: Workspace, channel: ResolvedChannel) -> Path:
    """Write `channels/<name>/docker-compose.yml`. A half-written file is a dead channel."""
    rendered = render_compose(workspace, channel)
    atomic_write_lines(channel.compose_path, rendered.splitlines())
    return channel.compose_path


# --------------------------------------------------------------------------
# Command execution
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def check(self) -> "CommandResult":
        if not self.ok:
            detail = (self.stderr or self.stdout or "").strip().splitlines()
            raise SupervisorError(
                f"{' '.join(self.argv[:3])} failed (rc={self.returncode}): "
                + (detail[-1] if detail else "no output")
            )
        return self


Runner = Callable[[Sequence[str], "float | None"], Awaitable[CommandResult]]


async def run_command(
    argv: Sequence[str], timeout: float | None = DEFAULT_TIMEOUT
) -> CommandResult:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return CommandResult(tuple(argv), 127, "", f"{argv[0]} not found")
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return CommandResult(tuple(argv), 124, "", f"timed out after {timeout}s")
    return CommandResult(
        tuple(argv),
        process.returncode or 0,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


# --------------------------------------------------------------------------
# Container facts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ContainerInfo:
    name: str
    exists: bool = False
    running: bool = False
    status: str = "absent"
    exit_code: int | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True)
class ChannelContainers:
    composer: ContainerInfo
    liquidsoap: ContainerInfo

    @property
    def state(self) -> ChannelState:
        if self.composer.running:
            return ChannelState.RUNNING
        if self.composer.exists or self.liquidsoap.running:
            # An exited composer is a fault whatever its exit code: a dead
            # producer makes FFmpeg exit rc=0, which looks like success.
            return ChannelState.FAILED
        return ChannelState.STOPPED


@dataclass(frozen=True)
class LogTail:
    text: str
    source: str
    path: Path
    line_count: int
    container: str = ""
    detail: str = ""


# --------------------------------------------------------------------------
# Supervisor
# --------------------------------------------------------------------------


@contextmanager
def _override_file(name: str, container: str) -> Iterator[Path]:
    """A throwaway Compose override renaming the replacement's container.

    Written to a temp directory, not the channel directory: a second generated
    file under `channels/` would need a `.gitignore` change, and that file
    belongs to the lead.
    """
    directory = tempfile.mkdtemp(prefix=f"ambient-{name}-")
    try:
        path = Path(directory) / "compose.next.yml"
        path.write_text(
            yaml.safe_dump(
                {"services": {f"{name}{COMPOSER_SUFFIX}": {"container_name": container}}},
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        yield path
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@dataclass
class Supervisor:
    workspace: Workspace
    events: EventHub | None = None
    docker: str = "docker"
    runner: Runner = run_command
    takeover_timeout: float = TAKEOVER_TIMEOUT
    takeover_poll: float = 1.0
    composer_image: str = field(
        default_factory=lambda: (os.environ.get("AMBIENT_COMPOSER_IMAGE") or "").strip()
        or DEFAULT_COMPOSER_IMAGE
    )
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, repr=False)
    _probes: dict[str, ProbeResult] = field(default_factory=dict, repr=False)

    # ---------------------------------------------------------------- naming

    def project(self, name: str, *, slot_next: bool = False) -> str:
        return f"ambient-{name}{NEXT_SUFFIX if slot_next else ''}"

    def composer_container(self, name: str, *, slot_next: bool = False) -> str:
        return f"{name}{COMPOSER_SUFFIX}{NEXT_SUFFIX if slot_next else ''}"

    def liquidsoap_container(self, name: str) -> str:
        return f"{name}{LIQUIDSOAP_SUFFIX}"

    def lock(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())

    # -------------------------------------------------------------- plumbing

    def compose_argv(
        self,
        name: str,
        args: Sequence[str],
        *,
        slot_next: bool = False,
        extra_files: Sequence[Path] = (),
    ) -> list[str]:
        """Both env files, root first, so the channel file wins on shared keys."""
        directory = channel_directory(self.workspace, name)
        compose_file = directory / "docker-compose.yml"
        if not compose_file.is_file():
            raise ConfigError(
                f"channel {name!r}: {compose_file} is missing; compile the channel first"
            )
        root_env = self.workspace.root / ".env"
        argv = [
            self.docker,
            "compose",
            "--project-name",
            self.project(name, slot_next=slot_next),
            "--project-directory",
            str(directory),
        ]
        if root_env.is_file():
            argv += ["--env-file", str(root_env)]
        argv += ["--env-file", str(directory / ".env"), "--file", str(compose_file)]
        for path in extra_files:
            argv += ["--file", str(path)]
        return argv + list(args)

    async def compose(
        self,
        name: str,
        args: Sequence[str],
        *,
        slot_next: bool = False,
        extra_files: Sequence[Path] = (),
        timeout: float | None = DEFAULT_TIMEOUT,
    ) -> CommandResult:
        argv = self.compose_argv(name, args, slot_next=slot_next, extra_files=extra_files)
        return await self.runner(argv, timeout)

    async def docker_argv(
        self, args: Sequence[str], *, timeout: float | None = DEFAULT_TIMEOUT
    ) -> CommandResult:
        return await self.runner([self.docker, *args], timeout)

    # ------------------------------------------------------------ inspection

    async def probe_encoder(
        self, encoder: str, *, refresh: bool = False
    ) -> ProbeResult:
        """Test-encode inside the composer image, because that is where encoding runs.

        A cached result is reused: each probe is a container start. Listing an
        encoder is not evidence — h264_qsv is advertised with no Intel device
        present, and h264_nvenc fails `OpenEncodeSessionEx` on a mismatched
        driver — so only a real encode counts.
        """
        if not refresh and encoder in self._probes:
            return self._probes[encoder]
        argv = docker_probe_argv(self.composer_image, encoder, docker=self.docker)
        result = await self.runner(argv, PROBE_TIMEOUT)
        if result.ok:
            probe = ProbeResult(encoder, True)
        else:
            probe = ProbeResult(
                encoder, False, probe_failure_detail(result.stderr or result.stdout, result.returncode)
            )
        self._probes[encoder] = probe
        return probe

    async def probe_encoders(
        self, encoders: Sequence[str], *, refresh: bool = False
    ) -> list[ProbeResult]:
        return [await self.probe_encoder(e, refresh=refresh) for e in encoders]

    async def inspect(self, container: str) -> ContainerInfo:
        result = await self.docker_argv(
            ["inspect", "--type", "container", "--format", "{{json .State}}", container],
            timeout=20.0,
        )
        if not result.ok:
            return ContainerInfo(name=container)
        try:
            state = json.loads(result.stdout.strip() or "{}")
        except json.JSONDecodeError:
            return ContainerInfo(name=container)
        return ContainerInfo(
            name=container,
            exists=True,
            running=bool(state.get("Running")),
            status=str(state.get("Status", "unknown")),
            exit_code=state.get("ExitCode"),
            started_at=state.get("StartedAt"),
            finished_at=state.get("FinishedAt"),
        )

    async def live_slot(self, name: str) -> bool:
        """True when the `-next` slot holds the running composer."""
        return (await self.inspect(self.composer_container(name, slot_next=True))).running

    async def containers(self, name: str) -> ChannelContainers:
        slot_next = await self.live_slot(name)
        composer = await self.inspect(self.composer_container(name, slot_next=slot_next))
        if not composer.exists and slot_next:
            composer = await self.inspect(self.composer_container(name))
        return ChannelContainers(
            composer=composer,
            liquidsoap=await self.inspect(self.liquidsoap_container(name)),
        )

    async def cpu_usage(self, containers: Sequence[str]) -> dict[str, float]:
        """One `docker stats` for every container we care about, in cores."""
        if not containers:
            return {}
        result = await self.docker_argv(
            ["stats", "--no-stream", "--format", "{{.Name}} {{.CPUPerc}}", *containers],
            timeout=30.0,
        )
        usage: dict[str, float] = {}
        if not result.ok:
            return usage
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                usage[parts[0]] = round(float(parts[1].rstrip("%")) / 100.0, 3)
            except ValueError:
                continue
        return usage

    async def read_run_file(self, name: str, filename: str, *, tail_bytes: int = 8192) -> str:
        """Read a file out of the composer's run directory.

        Read through the container rather than the host path so this still works
        if the run directory is ever moved back inside the container.
        """
        if "/" in filename or filename.startswith("."):
            raise ValueError(f"invalid runtime filename {filename!r}")
        container = self.composer_container(name, slot_next=await self.live_slot(name))
        result = await self.docker_argv(
            ["exec", container, "tail", "-c", str(int(tail_bytes)), f"{RUN_DIR}/{name}/{filename}"],
            timeout=20.0,
        )
        return result.stdout if result.ok else ""

    # ---------------------------------------------------------- zmq control

    async def send_zmq(self, name: str, message: str, *, endpoint: str = "") -> str:
        """Send one runtime command to the channel's filtergraph.

        The `zmq` filter binds inside the composer's own network namespace, so
        the message is delivered by `docker exec` rather than a socket the
        backend holds. It is validated first either way: a malformed message
        aborts FFmpeg with SIGABRT, which is why `validate_message` is a
        security boundary and not a convenience.
        """
        validate_message(message)
        address = validate_address(endpoint or ZMQ_ENDPOINT)
        container = self.composer_container(name, slot_next=await self.live_slot(name))
        result = await self.docker_argv(
            ["exec", container, "python3", "-c", _ZMQ_SENDER, address, message],
            timeout=30.0,
        )
        reply = (result.stdout or result.stderr).strip()
        if not result.ok or reply != SUCCESS:
            raise ZmqCommandError(f"{message!r} -> {reply or 'no reply'}")
        return reply

    async def send_zmq_batch(self, name: str, messages: Sequence[str]) -> list[str]:
        """Throughput ceilings at ~one command per frame; keep batches small."""
        return [await self.send_zmq(name, message) for message in messages]

    # ------------------------------------------------------------- lifecycle

    async def _announce(self, name: str, state: ChannelState, **extra: object) -> None:
        if self.events is not None:
            await self.events.publish(
                CHANNEL_STATUS, {"state": state.value, **extra}, channel=name
            )

    async def start(self, name: str) -> CommandResult:
        async with self.lock(name):
            await self._announce(name, ChannelState.STARTING)
            result = (await self.compose(name, ["up", "-d", "--remove-orphans"])).check()
            await self._announce(name, ChannelState.RUNNING)
            return result

    async def stop(self, name: str) -> CommandResult:
        async with self.lock(name):
            # The replacement slot first, so a stop mid-restart leaves nothing behind.
            await self.compose(name, ["down", "--remove-orphans"], slot_next=True)
            result = (await self.compose(name, ["down", "--remove-orphans"])).check()
            await self._announce(name, ChannelState.STOPPED)
            return result

    async def restart(self, name: str) -> dict[str, object]:
        """Make-before-break: the replacement claims the relay path first.

        The template pins `container_name`, so the replacement runs as a second
        project with a renamed container. Which slot is live is derived from
        Docker rather than stored, so a backend crash cannot lose track of it.
        """
        async with self.lock(name):
            from_next = await self.live_slot(name)
            to_next = not from_next
            outgoing = self.composer_container(name, slot_next=from_next)
            incoming = self.composer_container(name, slot_next=to_next)

            await self._announce(name, ChannelState.STARTING, phase="make-before-break")
            # Liquidsoap stays in the canonical project: audio lives behind
            # Icecast and must not be disturbed by a compositor swap.
            await self.compose(name, ["up", "-d", "--no-deps", f"{name}{LIQUIDSOAP_SUFFIX}"])

            with _override_file(name, incoming) as override:
                (
                    await self.compose(
                        name,
                        ["up", "-d", "--no-deps", "--force-recreate", f"{name}{COMPOSER_SUFFIX}"],
                        slot_next=to_next,
                        extra_files=[override] if to_next else [],
                    )
                ).check()
                claimed = await self._await_takeover(name, incoming)
                await self.compose(
                    name,
                    ["rm", "--stop", "--force", f"{name}{COMPOSER_SUFFIX}"],
                    slot_next=from_next,
                    extra_files=[override] if from_next else [],
                )

            await self._announce(name, ChannelState.RUNNING, phase="restarted")
            return {"outgoing": outgoing, "incoming": incoming, "took_over": claimed}

    async def _await_takeover(
        self, name: str, container: str, timeout: float | None = None
    ) -> bool:
        """Wait for the replacement's `out_time` to advance, then hand over.

        Bounded: a replacement that never publishes must not keep the outgoing
        composer alive indefinitely, because two composers cost two composers.
        """
        from .watchdog import parse_progress  # local: watchdog imports this module

        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self.takeover_timeout if timeout is None else timeout)
        first: int | None = None
        while loop.time() < deadline:
            result = await self.docker_argv(
                ["exec", container, "tail", "-c", "4096", f"{RUN_DIR}/{name}/progress"],
                timeout=15.0,
            )
            sample = parse_progress(result.stdout) if result.ok else None
            if sample is not None:
                if first is None:
                    first = sample.out_time_us
                elif sample.out_time_us > first:
                    return True
            await asyncio.sleep(self.takeover_poll)
        LOG.warning("channel %s: replacement %s never advanced out_time", name, container)
        return False

    # ---------------------------------------------------------------- delete

    async def delete(self, name: str) -> Path:
        """Remove a stopped channel. Refuses while any container still exists."""
        directory = channel_directory(self.workspace, name)
        if not directory.is_dir():
            raise ConfigError(f"channel {name!r} does not exist")
        containers = await self.containers(name)
        if containers.composer.exists or containers.liquidsoap.exists:
            raise ChannelBusy(f"channel {name!r} still has containers; stop it before deleting")
        # Re-checked because this call removes a directory tree.
        if directory.parent != self.workspace.channels_dir.resolve():
            raise ConfigError(f"refusing to delete {directory}: not a direct child of channels/")
        shutil.rmtree(directory)
        await self._announce(name, ChannelState.STOPPED, deleted=True)
        return directory

    # -------------------------------------------------------------- log tail

    def log_path(self, name: str, service: str) -> Path:
        if service not in LOG_SERVICES:
            raise ConfigError(
                f"unknown log service {service!r}; expected one of "
                f"{' '.join(sorted(LOG_SERVICES))}"
            )
        channel_directory(self.workspace, name)  # validates the name
        return self.workspace.log_dir / name / f"{service}.log"

    def tail_log(self, name: str, service: str, lines: int = 200) -> list[str]:
        path = self.log_path(name, service)
        wanted = max(1, min(int(lines), 2000))
        if not path.is_file():
            return []
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - wanted * 400))
            text = handle.read().decode("utf-8", "replace")
        return text.splitlines()[-wanted:]

    async def log_container(self, name: str, service: str) -> str:
        """The container whose stdout carries this service, or '' if none does."""
        role = LOG_SERVICE_CONTAINERS.get(service, "")
        if role == "liquidsoap":
            return self.liquidsoap_container(name)
        if role == "composer":
            return self.composer_container(name, slot_next=await self.live_slot(name))
        return ""

    async def read_log(self, name: str, service: str, lines: int = 200) -> "LogTail":
        """The file if it has content, else `docker logs`.

        Nothing under `/var/log/ambient` is written today — every process logs
        to stdout — so the file alone shows the operator an empty box.
        """
        path = self.log_path(name, service)
        wanted = max(1, min(int(lines), 2000))
        from_file = self.tail_log(name, service, wanted)
        if from_file:
            return LogTail("\n".join(from_file), "file", path, len(from_file))

        container = await self.log_container(name, service)
        if not container:
            return LogTail("", "none", path, 0)
        result = await self.docker_argv(
            ["logs", "--tail", str(wanted), container], timeout=30.0
        )
        if not result.ok:
            return LogTail("", "none", path, 0, detail=(result.stderr or "").strip())
        # FFmpeg writes progress to stderr, so both streams matter.
        text = "\n".join(part for part in (result.stdout, result.stderr) if part.strip())
        body = text.splitlines()[-wanted:]
        return LogTail("\n".join(body), "docker", path, len(body), container=container)

