"""Supervisor lifecycle: the compose invocation and the make-before-break swap.

The rendering half of these tests predates Phase 3 and is unchanged.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from ambient.config import ConfigError, load_workspace
from ambient.supervisor import Supervisor
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
