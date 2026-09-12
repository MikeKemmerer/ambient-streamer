"""Supervisor lifecycle: the compose invocation and the make-before-break swap.

The rendering half of these tests predates Phase 3 and is unchanged.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from ambient.config import ConfigError, load_workspace
from ambient.supervisor import CommandResult, Supervisor, SupervisorError
from ambient.zmqctl import ZmqValidationError
from tests.conftest import EXITED_STATE, RUNNING_STATE, FakeDocker


def supervisor(repo: Path, docker: FakeDocker) -> Supervisor:
    # Short waits: the takeover poll is a real 60 s wall clock in production.
    return Supervisor(
        workspace=load_workspace(repo), runner=docker, takeover_timeout=0.05, takeover_poll=0.01
    )


def flag_values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, token in enumerate(argv) if token == flag]


# --------------------------------------------------------------------------
# The invocation
# --------------------------------------------------------------------------


def test_both_env_files_are_passed_root_first(repo: Path, docker: FakeDocker) -> None:
    """Compose resolves the implicit .env relative to the compose file, so a
    channel file never sees the root one."""
    argv = supervisor(repo, docker).compose_argv("lofi", ["up", "-d"])
    env_files = flag_values(argv, "--env-file")

    assert env_files == [str(repo / ".env"), str(repo / "channels" / "lofi" / ".env")]
    assert flag_values(argv, "--project-name") == ["ambient-lofi"]
    assert flag_values(argv, "--file") == [
        str(repo / "channels" / "lofi" / "docker-compose.yml")
    ]
    assert flag_values(argv, "--project-directory") == [str(repo / "channels" / "lofi")]
    assert argv[:2] == ["docker", "compose"]


def test_a_missing_compose_file_is_refused(repo: Path, docker: FakeDocker) -> None:
    (repo / "channels" / "lofi" / "docker-compose.yml").unlink()
    with pytest.raises(ConfigError, match="compile the channel first"):
        supervisor(repo, docker).compose_argv("lofi", ["up"])


def test_a_hostile_channel_name_never_reaches_docker(repo: Path, docker: FakeDocker) -> None:
    sup = supervisor(repo, docker)
    for name in ("../etc", "lofi/../..", "Lofi", "", "a" * 64, "-lead"):
        with pytest.raises(ConfigError):
            sup.compose_argv(name, ["up"])
    assert docker.calls == []


def test_start_brings_the_project_up(repo: Path, docker: FakeDocker) -> None:
    asyncio.run(supervisor(repo, docker).start("lofi"))
    assert docker.compose_calls()[0][-3:] == ["up", "-d", "--remove-orphans"]


def test_visualizer_service_is_managed_in_the_channel_project(
    repo: Path, docker: FakeDocker
) -> None:
    sup = supervisor(repo, docker)
    argv = sup.compose_argv(
        "lofi", ["up", "-d", "--no-deps", "lofi-visualizer"]
    )

    assert flag_values(argv, "--project-name") == ["ambient-lofi"]
    assert flag_values(argv, "--file") == [
        str(repo / "channels" / "lofi" / "docker-compose.yml")
    ]
    assert argv[-4:] == ["up", "-d", "--no-deps", "lofi-visualizer"]


def test_stop_takes_down_both_slots(repo: Path, docker: FakeDocker) -> None:
    asyncio.run(supervisor(repo, docker).stop("lofi"))
    projects = [flag_values(call, "--project-name")[0] for call in docker.compose_calls()]
    assert projects == ["ambient-lofi-next", "ambient-lofi"]


def test_stop_never_reaches_the_global_stack(repo: Path, docker: FakeDocker) -> None:
    asyncio.run(supervisor(repo, docker).stop("lofi"))
    for call in docker.compose_calls():
        assert flag_values(call, "--project-name")[0].startswith("ambient-lofi")


# --------------------------------------------------------------------------
# Make-before-break
# --------------------------------------------------------------------------


def test_restart_starts_the_replacement_before_removing_the_outgoing(
    repo: Path, docker: FakeDocker
) -> None:
    docker.states["lofi-composer"] = RUNNING_STATE
    # The replacement publishes: out_time advances between the two reads.
    progress = iter(
        ["out_time_us=1000\nprogress=continue\n", "out_time_us=2000000\nprogress=continue\n"]
    )

    class Advancing(FakeDocker):
        async def __call__(self, argv, timeout=None):
            if list(argv)[1:2] == ["exec"]:
                self.calls.append(list(argv))
                from ambient.supervisor import CommandResult

                return CommandResult(tuple(argv), 0, next(progress, "progress=continue\n"), "")
            return await FakeDocker.__call__(self, argv, timeout)

    runner = Advancing()
    runner.states.update(docker.states)
    sup = Supervisor(
        workspace=load_workspace(repo), runner=runner, takeover_timeout=5.0, takeover_poll=0.01
    )
    result = asyncio.run(sup.restart("lofi"))

    order = [
        (flag_values(call, "--project-name")[0], call[-1] if call[-1] else "")
        for call in runner.compose_calls()
    ]
    up_next = next(i for i, (project, _) in enumerate(order) if project == "ambient-lofi-next")
    remove = next(
        i
        for i, call in enumerate(runner.compose_calls())
        if "rm" in call and "--stop" in call
    )
    assert up_next < remove, "the replacement must claim the path before the old one goes"
    assert result["incoming"] == "lofi-composer-next"
    assert result["outgoing"] == "lofi-composer"
    assert result["took_over"] is True


def test_restart_renames_the_replacement_container(repo: Path, docker: FakeDocker) -> None:
    from ambient.supervisor import _override_file

    with _override_file("lofi", "lofi-composer-next") as path:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert document["services"]["lofi-composer"]["container_name"] == "lofi-composer-next"


def test_restart_leaves_liquidsoap_alone(repo: Path, docker: FakeDocker) -> None:
    asyncio.run(supervisor(repo, docker).restart("lofi"))
    liquidsoap = [c for c in docker.compose_calls() if c[-1] == "lofi-liquidsoap"]
    assert liquidsoap and liquidsoap[0][-3:-1] == ["-d", "--no-deps"]
    assert not any("rm" in c and c[-1] == "lofi-liquidsoap" for c in docker.compose_calls())


def test_restart_alternates_slots(repo: Path, docker: FakeDocker) -> None:
    docker.states["lofi-composer-next"] = RUNNING_STATE
    result = asyncio.run(supervisor(repo, docker).restart("lofi"))
    assert result["outgoing"] == "lofi-composer-next"
    assert result["incoming"] == "lofi-composer"


# --------------------------------------------------------------------------
# Isolated visualizer
# --------------------------------------------------------------------------


class VisualizerDocker(FakeDocker):
    def __init__(self) -> None:
        super().__init__()
        self.ids = {"lofi-composer": "composer-1"}

    async def __call__(self, argv, timeout=None):
        args = list(argv)
        if args[1:2] == ["inspect"] and "{{.Id}}" in args:
            self.calls.append(args)
            container = args[-1]
            identity = self.ids.get(container)
            if identity is None:
                return CommandResult(tuple(args), 1, "", "No such object")
            return CommandResult(tuple(args), 0, identity, "")
        return await super().__call__(args, timeout)


def test_visualizer_lifecycle_targets_only_its_service(repo: Path) -> None:
    docker = VisualizerDocker()
    docker.states["lofi-composer"] = RUNNING_STATE
    sup = supervisor(repo, docker)

    started = asyncio.run(sup.start_visualizer("lofi"))
    recreated = asyncio.run(sup.recreate_visualizer("lofi"))
    stopped = asyncio.run(sup.stop_visualizer("lofi"))

    assert started.applied and recreated.applied and stopped.applied
    calls = docker.compose_calls()
    assert [call[-1] for call in calls] == ["lofi-visualizer"] * 3
    assert calls[0][-4:] == ["up", "-d", "--no-deps", "lofi-visualizer"]
    assert "--force-recreate" in calls[1]
    assert calls[2][-2:] == ["stop", "lofi-visualizer"]
    assert not any("lofi-composer" in call[-1:] for call in calls)


def test_stale_visualizer_generation_does_nothing(repo: Path) -> None:
    docker = VisualizerDocker()
    docker.states["lofi-composer"] = RUNNING_STATE
    sup = supervisor(repo, docker)
    stale = sup.next_visualizer_generation("lofi")
    current = sup.next_visualizer_generation("lofi")

    skipped = asyncio.run(sup.recreate_visualizer("lofi", generation=stale))
    applied = asyncio.run(sup.recreate_visualizer("lofi", generation=current))

    assert skipped.applied is False
    assert applied.applied is True
    assert len(docker.compose_calls()) == 1


def test_visualizer_action_fails_if_the_composer_changes(repo: Path) -> None:
    class ReplacingComposer(VisualizerDocker):
        async def __call__(self, argv, timeout=None):
            args = list(argv)
            result = await super().__call__(args, timeout)
            if args[1:2] == ["compose"]:
                self.ids["lofi-composer"] = "composer-2"
            return result

    docker = ReplacingComposer()
    docker.states["lofi-composer"] = RUNNING_STATE
    with pytest.raises(SupervisorError, match="composer changed"):
        asyncio.run(supervisor(repo, docker).recreate_visualizer("lofi"))


def test_visualizer_status_and_framekeeper_readiness(repo: Path) -> None:
    docker = VisualizerDocker()
    docker.states["lofi-composer"] = RUNNING_STATE
    docker.states["lofi-visualizer"] = RUNNING_STATE
    docker.files[
        "lofi-composer:/run/ambient/lofi/visualization-status.json"
    ] = (
        '{"ready":true,"fallback":false,"emitted":20,"received":18,'
        '"fallbacks":2,"reconnects":1,"last_frame_age_seconds":0.02,'
        '"updated_at":1789000000}'
    )
    sup = supervisor(repo, docker)

    status = asyncio.run(sup.visualizer_status("lofi"))
    readiness = asyncio.run(sup.visualizer_readiness("lofi"))

    assert status.running is True
    assert readiness.ready is True
    assert readiness.fallback is False
    assert readiness.received == 18
    assert readiness.last_frame_age_seconds == 0.02


# --------------------------------------------------------------------------
# Inspection
# --------------------------------------------------------------------------


def test_containers_reports_an_exited_composer(repo: Path, docker: FakeDocker) -> None:
    docker.states["lofi-composer"] = EXITED_STATE
    containers = asyncio.run(supervisor(repo, docker).containers("lofi"))
    assert containers.composer.exists is True
    assert containers.composer.running is False
    assert containers.composer.exit_code == 0
    assert containers.state.value == "failed"


def test_cpu_usage_parses_docker_stats(repo: Path) -> None:
    class Stats(FakeDocker):
        async def __call__(self, argv, timeout=None):
            from ambient.supervisor import CommandResult

            self.calls.append(list(argv))
            return CommandResult(tuple(argv), 0, "lofi-composer 148.60%\nlofi-liquidsoap 9.10%\n", "")

    usage = asyncio.run(supervisor(repo, Stats()).cpu_usage(["lofi-composer"]))
    assert usage == {"lofi-composer": 1.486, "lofi-liquidsoap": 0.091}


def test_a_runtime_filename_cannot_escape_the_run_directory(repo: Path, docker: FakeDocker) -> None:
    with pytest.raises(ValueError):
        asyncio.run(supervisor(repo, docker).read_run_file("lofi", "../../etc/passwd"))


# --------------------------------------------------------------------------
# ZMQ delivery
# --------------------------------------------------------------------------


def test_a_malformed_zmq_message_never_reaches_the_container(
    repo: Path, docker: FakeDocker
) -> None:
    """Measured: a message that does not parse into three tokens aborts FFmpeg
    with SIGABRT, so the validator runs before anything is sent."""
    sup = supervisor(repo, docker)
    for message in ("eq@eq", "", "   ", "eq@eq ", "nosuchfilter", "eq brightness 0.2"):
        with pytest.raises(ZmqValidationError):
            asyncio.run(sup.send_zmq("lofi", message))
    assert docker.calls == []


def test_a_valid_message_is_delivered_by_docker_exec(repo: Path) -> None:
    class Zmq(FakeDocker):
        async def __call__(self, argv, timeout=None):
            from ambient.supervisor import CommandResult

            self.calls.append(list(argv))
            if list(argv)[1:2] == ["exec"]:
                return CommandResult(tuple(argv), 0, "0 Success", "")
            return CommandResult(tuple(argv), 1, "", "No such object")

    runner = Zmq()
    reply = asyncio.run(supervisor(repo, runner).send_zmq("lofi", "eq@eq brightness 0.2"))
    assert reply == "0 Success"
    exec_call = [c for c in runner.calls if c[1:2] == ["exec"]][0]
    assert exec_call[2] == "lofi-composer"
    assert exec_call[-1] == "eq@eq brightness 0.2"
    assert exec_call[-2] == "tcp://127.0.0.1:5555"


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------


def test_the_log_service_name_is_a_whitelist(repo: Path, docker: FakeDocker) -> None:
    sup = supervisor(repo, docker)
    for service in ("../../etc/passwd", "compositor;rm", "", "shadow"):
        with pytest.raises(ConfigError, match="unknown log service"):
            sup.log_path("lofi", service)
    assert sup.log_path("lofi", "compositor").name == "compositor.log"


def test_tail_returns_the_last_lines(repo: Path, docker: FakeDocker) -> None:
    sup = supervisor(repo, docker)
    path = sup.log_path("lofi", "compositor")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"line {i}" for i in range(500)) + "\n", encoding="utf-8")
    assert sup.tail_log("lofi", "compositor", 5) == [f"line {i}" for i in range(495, 500)]
    assert sup.tail_log("lofi", "producer", 5) == []


def test_read_log_falls_back_to_docker_logs(repo: Path, docker: FakeDocker) -> None:
    """Nothing writes /var/log/ambient today; every process logs to stdout."""
    docker.logs["lofi-composer"] = "starting\nframe=1\n"
    tail = asyncio.run(supervisor(repo, docker).read_log("lofi", "compositor", 10))
    assert tail.source == "docker"
    assert tail.container == "lofi-composer"
    assert tail.text == "starting\nframe=1"
    assert tail.path.name == "compositor.log"
    assert docker.calls_matching("logs", "lofi-composer")


def test_read_log_uses_the_live_slot(repo: Path, docker: FakeDocker) -> None:
    docker.states["lofi-composer-next"] = RUNNING_STATE
    docker.logs["lofi-composer-next"] = "from the replacement\n"
    tail = asyncio.run(supervisor(repo, docker).read_log("lofi", "compositor", 10))
    assert tail.container == "lofi-composer-next"


def test_read_log_maps_liquidsoap_to_its_own_container(repo: Path, docker: FakeDocker) -> None:
    docker.logs["lofi-liquidsoap"] = "icecast_connected\n"
    tail = asyncio.run(supervisor(repo, docker).read_log("lofi", "liquidsoap", 10))
    assert tail.container == "lofi-liquidsoap"
    assert tail.text == "icecast_connected"


def test_read_log_reports_a_missing_container_rather_than_pretending(
    repo: Path, docker: FakeDocker
) -> None:
    tail = asyncio.run(supervisor(repo, docker).read_log("lofi", "compositor", 10))
    assert tail.source == "none"
    assert tail.text == ""
    assert "No such container" in tail.detail


def test_read_log_prefers_a_file_that_has_content(repo: Path, docker: FakeDocker) -> None:
    sup = supervisor(repo, docker)
    docker.logs["lofi-composer"] = "from docker\n"
    path = sup.log_path("lofi", "compositor")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("from the file\n", encoding="utf-8")
    tail = asyncio.run(sup.read_log("lofi", "compositor", 10))
    assert tail.source == "file"
    assert tail.text == "from the file"
    assert not docker.calls_matching("logs")


# --------------------------------------------------------------------------
# Encoder probes
# --------------------------------------------------------------------------


def test_the_probe_runs_inside_the_composer_image(repo: Path, docker: FakeDocker) -> None:
    """The backend image has no ffmpeg; the composer is where encoding happens."""
    sup = supervisor(repo, docker)
    result = asyncio.run(sup.probe_encoder("libx264"))
    assert result.available is True
    argv = docker.calls_matching("run")[0]
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "ambient-composer:dev" in argv
    assert "-f" in argv and "null" in argv


def test_a_failed_test_encode_reports_the_reason(repo: Path, docker: FakeDocker) -> None:
    from ambient.supervisor import CommandResult

    docker.runs["h264_nvenc"] = CommandResult(
        (),
        1,
        "",
        "[h264_nvenc @ 0x55] Cannot load libnvidia-encode.so.1\n"
        "[h264_nvenc @ 0x55] OpenEncodeSessionEx failed: unsupported device (2)\n",
    )
    result = asyncio.run(supervisor(repo, docker).probe_encoder("h264_nvenc"))
    assert result.available is False
    assert result.detail == "[h264_nvenc @ 0x55] OpenEncodeSessionEx failed: unsupported device (2)"


def test_a_probe_with_no_output_still_reports_something(repo: Path, docker: FakeDocker) -> None:
    from ambient.supervisor import CommandResult

    docker.runs["h264_qsv"] = CommandResult((), 125, "", "")
    result = asyncio.run(supervisor(repo, docker).probe_encoder("h264_qsv"))
    assert result.available is False
    assert result.detail == "rc=125"


def test_probes_are_cached_and_refreshable(repo: Path, docker: FakeDocker) -> None:
    sup = supervisor(repo, docker)
    asyncio.run(sup.probe_encoders(["libx264", "h264_nvenc"]))
    assert len(docker.calls_matching("run")) == 2
    asyncio.run(sup.probe_encoders(["libx264", "h264_nvenc"]))
    assert len(docker.calls_matching("run")) == 2
    asyncio.run(sup.probe_encoders(["libx264"], refresh=True))
    assert len(docker.calls_matching("run")) == 3


def test_the_probe_asks_for_the_hardware_the_encoder_needs(
    repo: Path, docker: FakeDocker
) -> None:
    sup = supervisor(repo, docker)
    asyncio.run(sup.probe_encoders(["libx264", "h264_nvenc", "h264_qsv"]))
    by_encoder = {c[c.index("-c:v") + 1]: c for c in docker.calls_matching("run")}
    assert "--gpus" in by_encoder["h264_nvenc"]
    assert "/dev/dri" in by_encoder["h264_qsv"]
    assert "--gpus" not in by_encoder["libx264"]
