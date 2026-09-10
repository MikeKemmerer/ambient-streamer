"""The handover watches the replacement, not the channel.

Both composer slots mount the same run directory, so `-progress` to one filename
had two writers during a make-before-break restart. The takeover check was then
reading whatever landed in that file - usually the OUTGOING composer, which is
streaming happily - and concluding the replacement had taken over before it had
produced a single frame. The outgoing composer was removed immediately and the
gap became the replacement's whole cold start, measured on the relay at 3.2s and
6.4s between `runOnReady stopped` and `runOnReady started`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from ambient.config import load_workspace
from ambient.supervisor import (
    PROGRESS_LIVE,
    PROGRESS_NEXT,
    CommandResult,
    Supervisor,
    _override_file,
    progress_name,
)
from tests.conftest import RUNNING_STATE, FakeDocker


def test_the_two_slots_write_different_files() -> None:
    assert progress_name(False) == PROGRESS_LIVE
    assert progress_name(True) == PROGRESS_NEXT
    assert PROGRESS_LIVE != PROGRESS_NEXT


def test_the_replacement_is_told_to_use_its_own_file() -> None:
    with _override_file("lofi", "lofi-composer-next") as path:
        service = yaml.safe_load(path.read_text(encoding="utf-8"))["services"]["lofi-composer"]
    assert service["container_name"] == "lofi-composer-next"
    assert service["environment"]["PROGRESS_NAME"] == PROGRESS_NEXT


def test_the_takeover_check_reads_the_replacements_file(repo: Path) -> None:
    """Reading the shared name would be watching the outgoing composer."""
    docker = FakeDocker()
    docker.states["lofi-composer"] = RUNNING_STATE
    advancing = iter(["out_time_us=1000\n", "out_time_us=2000000\n"])

    class Recording(FakeDocker):
        async def __call__(self, argv, timeout=None):
            if list(argv)[1:2] == ["exec"]:
                self.calls.append(list(argv))
                return CommandResult(tuple(argv), 0, next(advancing, "out_time_us=9000000\n"), "")
            return await FakeDocker.__call__(self, argv, timeout)

    runner = Recording()
    runner.states.update(docker.states)
    sup = Supervisor(
        workspace=load_workspace(repo), runner=runner, takeover_timeout=5.0, takeover_poll=0.01
    )
    asyncio.run(sup.restart("lofi"))

    tails = [call for call in runner.calls if "exec" in call and "tail" in call]
    assert tails, "the takeover check must have read something"
    watched = [c[-1] for c in tails]
    assert all(path.endswith(f"/{PROGRESS_NEXT}") for path in watched), watched
    assert all("lofi-composer-next" in call for call in tails), (
        "and it must read it out of the replacement's container"
    )


def test_reading_progress_prefers_the_live_slots_file(repo: Path) -> None:
    """Callers ask for 'progress' and mean 'whichever composer is on air'."""
    docker = FakeDocker()
    docker.states["lofi-composer-next"] = RUNNING_STATE
    sup = Supervisor(workspace=load_workspace(repo), runner=docker)

    asyncio.run(sup.read_run_file("lofi", "progress"))

    tails = [call for call in docker.calls if "exec" in call and "tail" in call]
    assert tails
    assert tails[0][-1].endswith(f"/{PROGRESS_NEXT}"), "the slot's own file comes first"
    assert "lofi-composer-next" in tails[0]


def test_an_empty_slot_file_falls_back_to_the_canonical_name(repo: Path) -> None:
    """A composer predating per-slot files still writes `progress`.

    A watchdog that cannot find progress restarts a healthy channel in a loop,
    which is exactly what happened on the deploy that introduced this.
    """
    docker = FakeDocker()
    docker.states["lofi-composer-next"] = RUNNING_STATE
    sup = Supervisor(workspace=load_workspace(repo), runner=docker)

    asyncio.run(sup.read_run_file("lofi", "progress"))

    read = [call[-1] for call in docker.calls if "exec" in call and "tail" in call]
    assert any(path.endswith(f"/{PROGRESS_NEXT}") for path in read)
    assert any(path.endswith(f"/{PROGRESS_LIVE}") for path in read), (
        "an empty slot file must not leave the watchdog blind"
    )


def test_reading_progress_on_the_canonical_slot_is_unchanged(repo: Path) -> None:
    docker = FakeDocker()
    docker.states["lofi-composer"] = RUNNING_STATE
    sup = Supervisor(workspace=load_workspace(repo), runner=docker)

    asyncio.run(sup.read_run_file("lofi", "progress"))

    tails = [call for call in docker.calls if "exec" in call and "tail" in call]
    assert tails[-1][-1].endswith(f"/{PROGRESS_LIVE}")


def test_other_run_files_are_not_rewritten(repo: Path) -> None:
    """Only progress is per-slot; now.json and friends stay where they are."""
    docker = FakeDocker()
    docker.states["lofi-composer-next"] = RUNNING_STATE
    sup = Supervisor(workspace=load_workspace(repo), runner=docker)

    asyncio.run(sup.read_run_file("lofi", "now.json"))

    tails = [call for call in docker.calls if "exec" in call and "tail" in call]
    assert tails[-1][-1].endswith("/now.json")
