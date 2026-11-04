"""Concurrent project sessions, each with its own backend and async lock."""

from __future__ import annotations

import asyncio
import contextvars
import time
from dataclasses import dataclass, field
from typing import Any

import config

from .com_watchdog import DialogWatchdog
from .staging import StagingManager, _safe_project_id

CURRENT_PROJECT_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "acadmcp_project_id", default=None
)


@dataclass
class ProjectSession:
    project_id: str
    backend: Any
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    staging: StagingManager | None = None
    watchdog: DialogWatchdog | None = None
    window_mode: str = "virtual_desktop"
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_used = time.time()

    def to_dict(self, active: bool = False) -> dict:
        pid = getattr(self.backend, "pid", None)
        return {
            "project_id": self.project_id,
            "backend": getattr(self.backend, "name", "unknown"),
            "active": active,
            "connected": bool(getattr(self.backend, "is_connected", False)),
            "window_mode": self.window_mode,
            "pid": pid,
            "staged": self.staging.current.to_dict()
            if self.staging and self.staging.current
            else None,
            "watchdog_events": list(self.watchdog.watchdog_events) if self.watchdog else [],
            "created_at": self.created_at,
            "last_used": self.last_used,
        }


class ProjectSessionManager:
    """Create, route and close independent CAD sessions.

    The manager lock protects only the session map. Actual tool calls hold the
    individual session's lock, so two project IDs never serialize each other.
    """

    def __init__(self):
        self.sessions: dict[str, ProjectSession] = {}
        self.active_project = "default"
        self._map_lock = asyncio.Lock()

    def register_default(self, backend: Any) -> ProjectSession:
        session = ProjectSession(
            project_id="default",
            backend=backend,
            staging=StagingManager("default"),
            window_mode=config.settings.background_window_mode,
        )
        if backend.name == "com" and config.settings.dialog_watchdog_enabled:
            session.watchdog = DialogWatchdog(
                "default", interval=config.settings.dialog_watchdog_interval_sec
            )
            session.watchdog.start()
        self.sessions["default"] = session
        self.active_project = "default"
        return session

    async def create(
        self,
        project_id: str,
        backend_name: str = "com",
        window_mode: str = "virtual_desktop",
    ) -> ProjectSession:
        project_id = _safe_project_id(project_id)
        backend_name = backend_name.lower().strip()
        if backend_name not in {"com", "ezdxf"}:
            raise ValueError("backend must be 'com' or 'ezdxf'")
        if window_mode not in {"virtual_desktop", "bottom", "offscreen", "hidden"}:
            raise ValueError("window_mode must be virtual_desktop, bottom, offscreen or hidden")
        async with self._map_lock:
            existing = self.sessions.get(project_id)
            if existing is not None:
                existing.touch()
                self.active_project = project_id
                return existing

            shared = None
            if backend_name == "com" and config.settings.single_window_per_project:
                shared = next(
                    (
                        candidate
                        for candidate in self.sessions.values()
                        if candidate.backend.name == "com"
                        and candidate.backend.is_connected
                    ),
                    None,
                )
            com_count = len(
                {id(s.backend) for s in self.sessions.values() if s.backend.name == "com"}
            )
            if (
                backend_name == "com"
                and shared is None
                and com_count >= config.settings.max_concurrent_cad_instances
            ):
                raise RuntimeError(
                    "MAX_CONCURRENT_CAD_INSTANCES="
                    f"{config.settings.max_concurrent_cad_instances} reached"
                )
            if shared is not None:
                # Project IDs are routing/staging namespaces in one OpenCode
                # workspace, not requests for additional AutoCAD processes.
                backend = shared.backend
                session = ProjectSession(
                    project_id=project_id,
                    backend=backend,
                    lock=shared.lock,
                    staging=StagingManager(project_id),
                    watchdog=shared.watchdog,
                    window_mode=shared.window_mode,
                )
                await backend.ensure_started()
            else:
                if backend_name == "com":
                    from .com_backend import ComBackend

                    backend = ComBackend(project_id=project_id, window_mode=window_mode)
                else:
                    from .ezdxf_backend import EzdxfBackend

                    backend = EzdxfBackend()
                session = ProjectSession(
                    project_id=project_id,
                    backend=backend,
                    staging=StagingManager(project_id),
                    window_mode=window_mode,
                )
                try:
                    await backend.connect()
                    if backend_name == "com":
                        await backend.ensure_started()
                    if backend_name == "com" and config.settings.dialog_watchdog_enabled:
                        session.watchdog = DialogWatchdog(
                            project_id,
                            interval=config.settings.dialog_watchdog_interval_sec,
                        )
                        session.watchdog.start()
                except BaseException:
                    # A spawned process may exist even when startup failed. Disconnect
                    # the unregistered backend now; never leave an orphan outside the map.
                    if session.watchdog is not None:
                        session.watchdog.stop()
                    try:
                        await backend.disconnect()
                    except Exception:
                        pass
                    raise
            self.sessions[project_id] = session
            self.active_project = project_id
            return session

    def resolve_id(self, project_id: str | None = None) -> str:
        selected = project_id or CURRENT_PROJECT_ID.get() or self.active_project or "default"
        return _safe_project_id(selected)

    def get(self, project_id: str | None = None) -> ProjectSession:
        selected = self.resolve_id(project_id)
        try:
            session = self.sessions[selected]
        except KeyError as exc:
            raise KeyError(f"Unknown project session {selected!r}; create it first") from exc
        session.touch()
        return session

    def backend_for(self, project_id: str | None = None) -> Any:
        return self.get(project_id).backend

    def switch(self, project_id: str) -> dict:
        session = self.get(project_id)
        self.active_project = session.project_id
        return session.to_dict(active=True)

    def list(self) -> dict:
        self.reap_idle_sync_safe()
        return {
            "ok": True,
            "active_project": self.active_project,
            "count": len(self.sessions),
            "projects": [
                session.to_dict(active=session.project_id == self.active_project)
                for _, session in sorted(self.sessions.items())
            ],
        }

    async def close(self, project_id: str, save: bool = True) -> dict:
        session = self.get(project_id)
        async with session.lock:
            if save:
                try:
                    await session.backend.drawing_save()
                except Exception:
                    # An empty/new session may have no document to save. The
                    # backend disconnect still owns the authoritative cleanup.
                    pass
            last_window_owner = not any(
                other is not session and other.backend is session.backend
                for other in self.sessions.values()
            )
            if last_window_owner:
                if session.watchdog:
                    session.watchdog.stop()
                await session.backend.disconnect()
        async with self._map_lock:
            self.sessions.pop(session.project_id, None)
            if self.active_project == session.project_id:
                self.active_project = next(iter(sorted(self.sessions)), "default")
        return {
            "ok": True,
            "project_id": session.project_id,
            "closed": True,
            "saved": save,
            "window_closed": last_window_owner,
        }

    async def close_all(self) -> None:
        for project_id in list(self.sessions):
            if project_id in self.sessions:
                await self.close(project_id, save=False)

    def reap_idle_sync_safe(self) -> None:
        """Idle expiry is performed by the async server path, never a second loop."""
        # Listing is synchronous and must not create tasks on an arbitrary loop.
        # Project routing calls reap_idle() before selecting a backend instead.
        return None

    async def reap_idle(self) -> None:
        timeout = config.settings.idle_instance_timeout
        if timeout <= 0:
            return
        now = time.time()
        expired = [
            project_id
            for project_id, session in self.sessions.items()
            if project_id != self.active_project
            and project_id != "default"
            and not session.lock.locked()
            and now - session.last_used >= timeout
        ]
        for project_id in expired:
            if project_id in self.sessions:
                await self.close(project_id, save=True)
