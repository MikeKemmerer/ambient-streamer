"""FastAPI control plane.

Implements docs/contracts/rest-api.md. Two rules shape the module:

* **Everything except `GET /api/health` needs a bearer token.** The process
  mounts the Docker socket, which is root-equivalent on the host, so the app
  refuses to start when `AMBIENT_API_TOKEN` is unset while bound to anything
  other than loopback.
* **A request that changes what is on air returns 202 and emits SSE.** It never
  blocks on the media pipeline.

The operator UI is served from `AMBIENT_FRONTEND_DIR` at `/`, mounted last so it
cannot shadow an API route, and unauthenticated because a browser cannot attach a
bearer token to the navigation that fetches the page. The token guards `/api/*`,
which is where the privilege lives; the assets themselves hold no secrets.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Iterable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from .config import (
    ConfigError,
    ResolvedChannel,
    Workspace,
    channel_directory,
    discover_channels,
    load_channel,
    load_workspace,
)
from .events import EventHub
from .media import MediaError
from .presets import PresetError
from .scheduler import ChannelSchedule, Scheduler
from .supervisor import ChannelBusy, Supervisor, SupervisorError
from .watchdog import Watchdog

LOG = logging.getLogger("ambient.main")

DEFAULT_BIND_ADDRESS = "127.0.0.1"
# Where docker/Dockerfile.backend bakes frontend/ and points AMBIENT_FRONTEND_DIR.
IMAGE_FRONTEND_DIR = Path("/opt/ambient/frontend")
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
RELAY_API_PORT = 9997
RELAY_TIMEOUT = 2.0
# Once the relay is unreachable, stop asking for a while. A name that does not
# resolve can block for far longer than the socket timeout, which is a DNS
# lookup, not a connection.
RELAY_BACKOFF = 30.0


class ApiError(HTTPException):
    """`error` is a stable machine token, `detail` is for humans."""

    def __init__(self, status_code: int, error: str, detail: str = "") -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.error = error


class StartupRefused(RuntimeError):
    """The process would expose a root-equivalent socket without a token."""


def _env(name: str, fallback: str = "") -> str:
    return (os.environ.get(name) or "").strip() or fallback


@dataclass
class AppState:
    root: Path
    workspace: Workspace
    events: EventHub
    supervisor: Supervisor
    watchdog: Watchdog
    scheduler: Scheduler
    token: str
    bind_address: str = DEFAULT_BIND_ADDRESS
    started_at: float = field(default_factory=time.time)
    cpu: dict[str, float] = field(default_factory=dict)
    relay_blocked_until: float = 0.0

    # ------------------------------------------------------------ workspace

    def reload(self) -> None:
        """`ambient.yaml` and the root `.env` are re-read on change."""
        self.workspace = load_workspace(self.root)
        self.supervisor.workspace = self.workspace
        self.watchdog.workspace = self.workspace

    def names(self) -> list[str]:
        try:
            return discover_channels(self.workspace)
        except ConfigError:
            return []

    def channel(self, name: str, *, resolve_media: bool = True) -> ResolvedChannel:
        try:
            channel_directory(self.workspace, name)
        except ConfigError as exc:
            raise ApiError(400, "invalid_channel_name", str(exc)) from exc
        if name not in self.names():
            raise ApiError(404, "unknown_channel", f"no channel named {name!r}")
        try:
            return load_channel(self.workspace, name, resolve_media=resolve_media)
        except (ConfigError, MediaError) as exc:
            raise ApiError(400, "invalid_channel_config", str(exc)) from exc

    def schedules(self) -> Iterable[ChannelSchedule]:
        for name in self.names():
            try:
                channel = load_channel(self.workspace, name, resolve_media=False)
            except (ConfigError, MediaError):
                continue
            yield ChannelSchedule(
                name=name,
                schedule=channel.config.schedule,
                default=channel.config.preset,
                applied=channel.config.preset,
            )

    # ---------------------------------------------------------------- relay

    @property
    def relay_api(self) -> str:
        host = urlsplit(self.workspace.ambient.relay.hls).hostname or "mediamtx"
        return f"http://{host}:{RELAY_API_PORT}"

    async def relay_path(self, path: str) -> dict[str, Any] | None:
        loop = asyncio.get_running_loop()
        if loop.time() < self.relay_blocked_until:
            return None
        future = _in_daemon_thread(_fetch_json, f"{self.relay_api}/v3/paths/get/{path}")
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), RELAY_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError, OSError):
            self.relay_blocked_until = loop.time() + RELAY_BACKOFF
            return None

    async def relay_paths(self) -> dict[str, dict[str, Any]] | None:
        """Every relay path in one call, so listing N channels is not 2N calls.

        `None` means the relay could not be asked, which is not the same as a
        path being absent — the caller must report that difference rather than
        claiming everything is down.
        """
        loop = asyncio.get_running_loop()
        if loop.time() < self.relay_blocked_until:
            return None
        future = _in_daemon_thread(_fetch_json, f"{self.relay_api}/v3/paths/list")
        try:
            payload = await asyncio.wait_for(asyncio.wrap_future(future), RELAY_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError, OSError):
            self.relay_blocked_until = loop.time() + RELAY_BACKOFF
            return None
        items = (payload or {}).get("items")
        if not isinstance(items, list):
            return None
        return {
            item["name"]: item
            for item in items
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }

    async def refresh_cpu(self) -> None:
        containers: list[str] = []
        for name in self.names():
            containers.append(self.supervisor.liquidsoap_container(name))
            containers.append(self.supervisor.composer_container(name))
            containers.append(self.supervisor.composer_container(name, slot_next=True))
        self.cpu = await self.supervisor.cpu_usage(containers)

    def channel_cores(self, name: str) -> float:
        prefix = f"{name}-"
        return round(sum(v for k, v in self.cpu.items() if k.startswith(prefix)), 3)


def _in_daemon_thread(func, *args) -> concurrent.futures.Future:
    """Run a blocking call where abandoning it cannot hold up shutdown."""
    future: concurrent.futures.Future = concurrent.futures.Future()

    def work() -> None:
        try:
            result: Any = func(*args)
        except BaseException as exc:  # noqa: BLE001 - relayed to the caller
            result = exc
        with suppress(concurrent.futures.InvalidStateError):
            if isinstance(result, BaseException):
                future.set_exception(result)
            else:
                future.set_result(result)

    threading.Thread(target=work, daemon=True, name="ambient-probe").start()
    return future


def _fetch_json(url: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=RELAY_TIMEOUT) as response:  # noqa: S310
            if response.status != 200:
                return None
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def resolve_token(workspace: Workspace) -> str:
    return _env("AMBIENT_API_TOKEN", workspace.env.api_token.get_secret_value())


def resolve_bind_address(workspace: Workspace) -> str:
    return _env("AMBIENT_BIND_ADDRESS", workspace.env.bind_address or DEFAULT_BIND_ADDRESS)


def check_exposure(bind_address: str, token: str) -> None:
    if not token and bind_address not in LOOPBACK:
        raise StartupRefused(
            f"AMBIENT_API_TOKEN is unset while bound to {bind_address!r}. This "
            "process holds the Docker socket, which is root-equivalent on the "
            "host; set a token (openssl rand -hex 32) or bind to 127.0.0.1."
        )


def build_state(root: Path) -> AppState:
    workspace = load_workspace(root)
    token = resolve_token(workspace)
    bind_address = resolve_bind_address(workspace)
    check_exposure(bind_address, token)
    if not token:
        LOG.warning("AMBIENT_API_TOKEN is unset; the API is open on %s", bind_address)

    events = EventHub()
    supervisor = Supervisor(workspace=workspace, events=events)
    watchdog = Watchdog(workspace=workspace, supervisor=supervisor, events=events)
    scheduler = Scheduler(discover=tuple, apply=_unwired_apply)
    state = AppState(
        root=root,
        workspace=workspace,
        events=events,
        supervisor=supervisor,
        watchdog=watchdog,
        scheduler=scheduler,
        token=token,
        bind_address=bind_address,
    )
    # The scheduler reads and writes the state it lives in, so it is wired here.
    scheduler.discover = state.schedules
    scheduler.apply = _preset_applier(state)
    return state


async def _unwired_apply(_channel: str, _preset: str) -> None:  # pragma: no cover
    raise RuntimeError("the scheduler was started before it was wired")


def _preset_applier(state: AppState):
    async def apply(name: str, preset: str) -> None:
        from .api.looks import apply_preset_to_channel

        await apply_preset_to_channel(state, name, preset)

    return apply


def repo_root(explicit: Path | str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = _env("AMBIENT_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def resolve_frontend_dir(root: Path) -> Path:
    """`AMBIENT_FRONTEND_DIR`, else the baked image path, else the checkout."""
    explicit = _env("AMBIENT_FRONTEND_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    if IMAGE_FRONTEND_DIR.is_dir():
        return IMAGE_FRONTEND_DIR
    return (root / "frontend").resolve()


def mount_frontend(app: FastAPI, directory: Path) -> bool:
    """Mount the UI at `/`. Call last: this route matches every unclaimed path."""
    if not directory.is_dir():
        LOG.warning(
            "operator UI not served: %s is not a directory. The API is unaffected; "
            "set AMBIENT_FRONTEND_DIR to the frontend/ directory to serve it.",
            directory,
        )
        return False
    app.mount("/", StaticFiles(directory=directory, html=True), name="frontend")
    LOG.info("serving the operator UI from %s", directory)
    return True


def create_app(root: Path | str | None = None) -> FastAPI:
    from .api import bumpers, channels, directory, looks, media, preview, system

    resolved_root = repo_root(root)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state = build_state(resolved_root)
        app.state.ambient = state
        tasks = [
            state.watchdog.start(state.names),
            state.scheduler.start(),
            asyncio.create_task(_cpu_loop(state), name="ambient-cpu"),
        ]
        LOG.info("ambient backend ready on %s", state.bind_address)
        try:
            yield
        finally:
            await state.watchdog.stop()
            await state.scheduler.stop()
            for task in tasks:
                task.cancel()
            await state.events.close()

    app = FastAPI(
        title="ambient-streamer control plane",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.error, "detail": str(exc.detail or "")},
        )

    @app.exception_handler(HTTPException)
    async def _http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        # Starlette's own 404 for an unmatched path lands here too, so every
        # error the app can produce carries the contract shape.
        error = {400: "bad_request", 401: "unauthorized", 404: "not_found", 409: "conflict"}.get(
            exc.status_code, "request_failed"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": error, "detail": str(exc.detail or "")},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_request",
                "detail": f"{location or 'body'}: {first.get('msg', 'invalid request')}",
            },
        )

    @app.exception_handler(ConfigError)
    async def _config_error(_request: Request, exc: ConfigError) -> JSONResponse:
        return JSONResponse(
            status_code=400, content={"error": "invalid_config", "detail": str(exc)}
        )

    @app.exception_handler(MediaError)
    async def _media_error(_request: Request, exc: MediaError) -> JSONResponse:
        return JSONResponse(
            status_code=400, content={"error": "invalid_media_path", "detail": str(exc)}
        )

    @app.exception_handler(PresetError)
    async def _preset_error(_request: Request, exc: PresetError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": "invalid_preset", "detail": str(exc)})

    @app.exception_handler(ChannelBusy)
    async def _busy(_request: Request, exc: ChannelBusy) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": "channel_busy", "detail": str(exc)})

    @app.exception_handler(SupervisorError)
    async def _supervisor_error(_request: Request, exc: SupervisorError) -> JSONResponse:
        return JSONResponse(
            status_code=503, content={"error": "supervisor_failed", "detail": str(exc)}
        )

    app.include_router(system.router)
    app.include_router(channels.router)
    app.include_router(media.router)
    app.include_router(looks.router)
    app.include_router(bumpers.router)
    # Tokenless, and mapped onto the public root by the LAN front door.
    app.include_router(directory.router)
    # After the /api routers and before the mount: it claims /<channel>/preview/*.
    app.include_router(preview.router)
    # Last, so every route above wins the match before the catch-all mount.
    mount_frontend(app, resolve_frontend_dir(resolved_root))
    return app


async def _cpu_loop(state: AppState, interval: float = 30.0) -> None:
    while True:
        try:
            await state.refresh_cpu()
        except Exception:  # pragma: no cover - defensive
            LOG.debug("cpu sampling failed", exc_info=True)
        await asyncio.sleep(interval)


def compare_token(supplied: str, expected: str) -> bool:
    return bool(expected) and secrets.compare_digest(supplied, expected)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - entry point
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="ambient-backend", description=__doc__)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    root = repo_root(args.repo_root)
    workspace = load_workspace(root)
    host = args.host or resolve_bind_address(workspace)
    try:
        check_exposure(host, resolve_token(workspace))
    except StartupRefused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    port = args.port or int(_env("AMBIENT_BACKEND_PORT", str(workspace.env.backend_port)))
    uvicorn.run(create_app(root), host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
