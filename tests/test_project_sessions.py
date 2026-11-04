"""Project-session lifecycle and real FastMCP routing/concurrency tests."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from fastmcp import Client

import config
import server
from backends.project_sessions import ProjectSession, ProjectSessionManager
from backends.staging import StagingManager

pytestmark = pytest.mark.asyncio


class _FakeComBackend:
    name = "com"
    fail_start = False

    def __init__(self, project_id="default", window_mode=None):
        self.project_id = project_id
        self.window_mode = window_mode
        self.is_connected = False
        self.is_attached_existing = False
        self.pid = None
        self._executor = None
        self.disconnects = 0
        self.saves = 0

    async def connect(self):
        self._executor = object()
        self.is_connected = True

    async def ensure_started(self):
        if self.fail_start:
            raise RuntimeError("startup failed")
        self.pid = 1000 + int(self.project_id == "beta")
        return {"ok": True}

    async def attach_existing(self, target_pid=None, window_title=None, dwg_name=None):
        self.pid = target_pid or 4242
        self.is_attached_existing = True
        self.window_mode = "keep"
        return {"ok": True, "pid": self.pid}

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False
        self._executor = None

    async def drawing_save(self):
        self.saves += 1
        return {"ok": True}


@pytest.fixture
def fake_com(monkeypatch):
    _FakeComBackend.fail_start = False
    monkeypatch.setattr("backends.com_backend.ComBackend", _FakeComBackend)
    monkeypatch.setattr(config.settings, "dialog_watchdog_enabled", False)
    monkeypatch.setattr(config.settings, "max_concurrent_cad_instances", 4)


async def test_each_project_has_independent_backend_lock_staging_and_executor(fake_com, monkeypatch):
    monkeypatch.setenv("SINGLE_WINDOW_PER_PROJECT", "false")
    manager = ProjectSessionManager()

    alpha = await manager.create("alpha")
    beta = await manager.create("beta")

    assert alpha.backend is not beta.backend
    assert alpha.lock is not beta.lock
    assert alpha.staging is not beta.staging
    assert alpha.staging.root != beta.staging.root
    assert alpha.backend._executor is not beta.backend._executor
    assert alpha.backend.pid != beta.backend.pid
    await manager.close_all()


async def test_explicit_attach_does_not_start_a_dialog_watchdog(fake_com, monkeypatch):
    monkeypatch.setenv("SINGLE_WINDOW_PER_PROJECT", "false")
    monkeypatch.setattr(config.settings, "dialog_watchdog_enabled", True)
    manager = ProjectSessionManager()

    session = await manager.attach("alpha", target_pid=4242)

    assert session.backend.is_attached_existing is True
    assert session.window_mode == "keep"
    assert session.watchdog is None
    await manager.close_all()


async def test_failed_session_startup_disconnects_unregistered_backend(fake_com):
    manager = ProjectSessionManager()
    _FakeComBackend.fail_start = True

    with pytest.raises(RuntimeError, match="startup failed"):
        await manager.create("alpha")

    assert manager.sessions == {}


async def test_idle_reaping_closes_only_expired_inactive_projects(fake_com, monkeypatch):
    monkeypatch.setenv("SINGLE_WINDOW_PER_PROJECT", "false")
    monkeypatch.setattr(config.settings, "idle_instance_timeout", 10)
    manager = ProjectSessionManager()
    alpha = await manager.create("alpha")
    beta = await manager.create("beta")
    manager.active_project = "beta"
    alpha.last_used = time.time() - 20
    beta.last_used = time.time()

    await manager.reap_idle()

    assert set(manager.sessions) == {"beta"}
    assert alpha.backend.disconnects == 1
    assert beta.backend.disconnects == 0
    await manager.close_all()


async def test_commit_requires_and_saves_the_bound_staged_document(tmp_path):
    manager = ProjectSessionManager()
    backend = _FakeComBackend("alpha")
    staging = StagingManager("alpha", tmp_path / "staging")
    session = ProjectSession("alpha", backend, staging=staging)
    manager.sessions["alpha"] = session
    source = tmp_path / "part.dwg"
    source.write_bytes(b"source")
    staged = staging.stage(source)
    staged.staged.write_bytes(b"edited")
    backend.document_identity = lambda: {"path": str(staged.staged)}
    ctx = SimpleNamespace(lifespan_context={"sessions": manager})

    result = await server.project_commit_staged("alpha", ctx=ctx)

    assert backend.saves == 1
    assert result["output_path"] == str(tmp_path / "part_AI_v2.dwg")
    assert (tmp_path / "part_AI_v2.dwg").read_bytes() == b"edited"
    assert source.read_bytes() == b"source"


async def test_commit_refuses_another_bound_document(tmp_path):
    manager = ProjectSessionManager()
    backend = _FakeComBackend("alpha")
    staging = StagingManager("alpha", tmp_path / "staging")
    session = ProjectSession("alpha", backend, staging=staging)
    manager.sessions["alpha"] = session
    source = tmp_path / "part.dwg"
    source.write_bytes(b"source")
    staging.stage(source)
    other = tmp_path / "other.dwg"
    other.write_bytes(b"other")
    backend.document_identity = lambda: {"path": str(other)}
    ctx = SimpleNamespace(lifespan_context={"sessions": manager})

    with pytest.raises(Exception, match="not bound to its staged DWG"):
        await server.project_commit_staged("alpha", ctx=ctx)
    assert backend.saves == 0


async def _create_headless_projects(client, *project_ids):
    for project_id in project_ids:
        result = await client.call_tool(
            "project_session_create", {"project_id": project_id, "backend": "ezdxf"}
        )
        assert result.is_error is False
        await client.call_tool("drawing_new", {"project_id": project_id})


async def test_real_mcp_client_routes_extra_project_id_and_lifecycle():
    async with Client(server.mcp) as client:
        await _create_headless_projects(client, "alpha", "beta")
        created = await client.call_tool(
            "entity_create_circle", {"cx": 10, "cy": 20, "radius": 5, "project_id": "alpha"}
        )
        assert created.is_error is False

        alpha = await client.call_tool("analysis_entity_stats", {"project_id": "alpha"})
        beta = await client.call_tool("analysis_entity_stats", {"project_id": "beta"})
        assert alpha.structured_content["total_entities"] == 1
        assert beta.structured_content["total_entities"] == 0

        switched = await client.call_tool("project_session_switch", {"project_id": "alpha"})
        assert switched.structured_content["active"] is True
        listed = await client.call_tool("project_session_list", {})
        assert listed.structured_content["active_project"] == "alpha"
        assert {row["project_id"] for row in listed.structured_content["projects"]} >= {
            "alpha",
            "beta",
        }
        closed = await client.call_tool(
            "project_session_close", {"project_id": "beta", "save": False}
        )
        assert closed.structured_content["closed"] is True


async def test_real_mcp_calls_overlap_across_projects_and_serialize_within_one(monkeypatch):
    sessions = {}
    original_create = ProjectSessionManager.create

    async def capture_create(self, *args, **kwargs):
        session = await original_create(self, *args, **kwargs)
        sessions[session.project_id] = session
        return session

    monkeypatch.setattr(ProjectSessionManager, "create", capture_create)
    async with Client(server.mcp) as client:
        await _create_headless_projects(client, "alpha", "beta")
        alpha = sessions["alpha"].backend
        beta = sessions["beta"].backend
        original_alpha = alpha.entity_create_circle
        original_beta = beta.entity_create_circle
        alpha_entered = asyncio.Event()
        alpha_release = asyncio.Event()
        beta_entered = asyncio.Event()
        alpha_calls = 0

        async def slow_alpha(*args, **kwargs):
            nonlocal alpha_calls
            alpha_calls += 1
            alpha_entered.set()
            await alpha_release.wait()
            return await original_alpha(*args, **kwargs)

        async def slow_beta(*args, **kwargs):
            beta_entered.set()
            return await original_beta(*args, **kwargs)

        alpha.entity_create_circle = slow_alpha
        beta.entity_create_circle = slow_beta
        arguments = {"cx": 1, "cy": 2, "radius": 3, "project_id": "alpha"}
        first = asyncio.create_task(client.call_tool("entity_create_circle", arguments))
        await asyncio.wait_for(alpha_entered.wait(), timeout=2)
        second = asyncio.create_task(client.call_tool("entity_create_circle", arguments))
        other = asyncio.create_task(
            client.call_tool(
                "entity_create_circle",
                {"cx": 4, "cy": 5, "radius": 6, "project_id": "beta"},
            )
        )

        await asyncio.wait_for(beta_entered.wait(), timeout=2)
        await asyncio.sleep(0.05)
        assert alpha_calls == 1, "the second alpha call passed the per-project lock"
        assert not second.done()

        alpha_release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second, other), timeout=5)
        assert all(not result.is_error for result in results)
        assert alpha_calls == 2


async def test_discovery_proxy_routes_project_id_without_recursive_deadlock():
    server._apply_discovery_mode("search")
    try:
        async with Client(server.mcp) as client:
            created = await asyncio.wait_for(
                client.call_tool(
                    "call_tool",
                    {
                        "name": "project_session_create",
                        "arguments": {"project_id": "alpha", "backend": "ezdxf"},
                    },
                ),
                timeout=5,
            )
            assert created.is_error is False
            await asyncio.wait_for(
                client.call_tool(
                    "call_tool",
                    {"name": "drawing_new", "arguments": {"project_id": "alpha"}},
                ),
                timeout=5,
            )
            info = await asyncio.wait_for(
                client.call_tool(
                    "call_tool",
                    {"name": "drawing_info", "arguments": {"project_id": "alpha"}},
                ),
                timeout=5,
            )
            assert info.is_error is False
    finally:
        server._apply_discovery_mode("off")
