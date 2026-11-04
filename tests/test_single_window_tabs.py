"""One agent-owned AutoCAD window per workspace, with independent MDI tabs."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
import server
from backends import com_backend as cb
from backends.project_sessions import ProjectSession, ProjectSessionManager
from backends.staging import StagingManager

pytestmark = pytest.mark.asyncio


class _Space:
    Count = 0


class _Doc:
    def __init__(self, app, name, path):
        self.app = app
        self.Name = name
        self.FullName = path
        self.Saved = True
        self.ModelSpace = _Space()
        self.vars = {"LTSCALE": 1.0}

    def Activate(self):
        self.app.ActiveDocument = self

    def Close(self, save):
        self.app.Documents.docs.remove(self)
        self.app.ActiveDocument = self.app.Documents.docs[-1] if self.app.Documents.docs else None

    def Save(self):
        self.Saved = True

    def SaveAs(self, path, save_as_type=None):
        self.FullName = str(path)
        self.Name = os.path.basename(str(path))

    def GetVariable(self, name):
        return self.vars.get(name, 0)

    def SetVariable(self, name, value):
        self.vars[name] = value


class _Documents:
    def __init__(self, app):
        self.app = app
        self.docs = []
        self.open_calls = []
        self.open_read_only = []
        self.add_calls = 0

    @property
    def Count(self):
        return len(self.docs)

    def Item(self, index):
        return self.docs[index]

    def Open(self, path, read_only=False):
        self.open_calls.append(str(path))
        self.open_read_only.append(bool(read_only))
        doc = _Doc(self.app, os.path.basename(str(path)), os.path.abspath(str(path)))
        self.docs.append(doc)
        self.app.ActiveDocument = doc
        return doc

    def Add(self, template=None):
        self.add_calls += 1
        doc = _Doc(self.app, f"Drawing{len(self.docs) + 1}.dwg", "")
        self.docs.append(doc)
        self.app.ActiveDocument = doc
        return doc


class _App:
    Version = "25.1s (test)"
    HWND = 5001

    def __init__(self):
        self.Documents = _Documents(self)
        self.ActiveDocument = None
        self.Preferences = SimpleNamespace(Files=SimpleNamespace(ProjectFilePath=""))
        self.quit_calls = 0

    def Quit(self):
        self.quit_calls += 1


def _com_backend(monkeypatch):
    app = _App()
    backend = cb.ComBackend("alpha")
    placements = []

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", run_inline)
    monkeypatch.setattr(backend, "_reapply_background_sync", lambda app: placements.append(app))
    monkeypatch.setattr(cb, "_acad_app", lambda: app)
    return backend, app, placements


class _ManagerCom:
    name = "com"
    instances = 0

    def __init__(self, project_id="default", window_mode=None):
        type(self).instances += 1
        self.project_id = project_id
        self.window_mode = window_mode
        self.is_connected = False
        self.pid = None
        self._executor = None
        self.disconnects = 0

    async def connect(self):
        self.is_connected = True
        self._executor = object()

    async def ensure_started(self):
        self.pid = 4242
        return {"ok": True}

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False
        self._executor = None

    async def drawing_save(self):
        return {"ok": True}


async def test_sessions_share_one_process_backend_sta_and_lock(monkeypatch):
    _ManagerCom.instances = 0
    monkeypatch.setattr("backends.com_backend.ComBackend", _ManagerCom)
    monkeypatch.setenv("SINGLE_WINDOW_PER_PROJECT", "true")
    monkeypatch.setattr(config.settings, "dialog_watchdog_enabled", False)
    manager = ProjectSessionManager()

    alpha = await manager.create("alpha")
    beta = await manager.create("beta")

    assert _ManagerCom.instances == 1
    assert alpha.backend is beta.backend
    assert alpha.backend._executor is beta.backend._executor
    assert alpha.lock is beta.lock
    assert alpha.backend.pid == beta.backend.pid == 4242
    assert alpha.staging is not beta.staging

    await manager.close("alpha", save=False)
    assert alpha.backend.disconnects == 0
    await manager.close("beta", save=False)
    assert alpha.backend.disconnects == 1


async def test_open_and_new_keep_tabs_and_revisions_per_document(monkeypatch, tmp_path):
    backend, app, placements = _com_backend(monkeypatch)
    first = tmp_path / "First.dwg"
    second = tmp_path / "Second.dwg"
    first.write_bytes(b"one")
    second.write_bytes(b"two")

    await backend.drawing_open(str(first))
    backend._bump_revision()
    first_identity = backend.document_identity()
    await backend.drawing_open(str(second))
    second_identity = backend.document_identity()

    assert app.Documents.Count == 2
    assert first_identity["document_id"] != second_identity["document_id"]
    assert first_identity["revision"] == 1
    assert second_identity["revision"] == 0

    # Opening an existing path activates its tab instead of reopening it.
    await backend.drawing_open(str(first))
    assert app.Documents.Count == 2
    assert len(app.Documents.open_calls) == 2
    assert backend.document_identity()["document_id"] == first_identity["document_id"]
    assert backend.document_identity()["revision"] == 1
    backend._bump_revision()
    assert backend.document_identity()["revision"] == 2

    await backend.document_activate("Second.dwg")
    assert backend.document_identity()["document_id"] == second_identity["document_id"]
    assert backend.document_identity()["revision"] == 0
    await backend.drawing_new()
    assert app.Documents.Count == 3
    assert app.Documents.add_calls == 1
    assert len(placements) == 5


async def test_tab_tools_resolve_source_names_and_indices_and_sync_staging(
    monkeypatch, tmp_path
):
    backend, app, _placements = _com_backend(monkeypatch)
    manager = ProjectSessionManager()
    staging = StagingManager("alpha", tmp_path / "staging")
    manager.sessions["alpha"] = ProjectSession("alpha", backend, staging=staging)
    manager.active_project = "alpha"
    ctx = SimpleNamespace(lifespan_context={"sessions": manager})
    sources = []
    for name in ("One.dwg", "Two.dwg"):
        source = tmp_path / name
        source.write_bytes(name.encode())
        sources.append(source)
        staged = staging.stage(source)
        await backend.drawing_open(str(staged.staged))

    listed = await server.drawing_list_tabs("alpha", ctx=ctx)
    assert listed["count"] == 2
    assert [row["tab_index"] for row in listed["tabs"]] == [0, 1]
    assert all(row["is_staged"] for row in listed["tabs"])
    assert listed["tabs"][1]["source_dwg_path"] == str(sources[1])
    assert listed["tabs"][1]["is_active"] is True

    switched = await server.drawing_switch_tab("One.dwg", "alpha", ctx=ctx)
    assert switched["active"] == "One.staged.dwg"
    assert staging.current.source == sources[0]
    await server.drawing_switch_tab("1", "alpha", ctx=ctx)
    assert staging.current.source == sources[1]

    closed = await server.drawing_close_tab("Two.dwg", save=True, project_id="alpha", ctx=ctx)
    assert closed["closed"] == "Two.staged.dwg"
    assert app.Documents.Count == 1
    assert staging.current.source == sources[0]


async def test_staged_tabs_diff_and_commit_independently(monkeypatch, tmp_path):
    backend, _app, _placements = _com_backend(monkeypatch)
    manager = ProjectSessionManager()
    staging = StagingManager("alpha", tmp_path / "staging")
    manager.sessions["alpha"] = ProjectSession("alpha", backend, staging=staging)
    manager.active_project = "alpha"
    ctx = SimpleNamespace(lifespan_context={"sessions": manager})
    staged_records = []
    for name, edited in (("One.dwg", b"edited-one"), ("Two.dwg", b"edited-two")):
        source = tmp_path / name
        source.write_bytes(name.encode())
        record = staging.stage(source)
        record.staged.write_bytes(edited)
        await backend.drawing_open(str(record.staged))
        staged_records.append(record)

    calls = []

    async def fake_diff(old_path, new_path, ctx):
        calls.append((old_path, new_path))
        return {"ok": True}

    monkeypatch.setattr(server, "drawing_diff", fake_diff)
    await server.project_diff_staged("alpha", "One.dwg", ctx=ctx)
    assert calls == [(str(staged_records[0].source), str(staged_records[0].staged))]

    result = await server.project_commit_staged("alpha", target_dwg="One.dwg", ctx=ctx)
    assert result["output_path"] == str(tmp_path / "One_AI_v2.dwg")
    assert (tmp_path / "One_AI_v2.dwg").read_bytes() == b"edited-one"
    assert not (tmp_path / "Two_AI_v2.dwg").exists()

    await server.project_commit_staged("alpha", target_dwg="Two.staged.dwg", ctx=ctx)
    assert (tmp_path / "Two_AI_v2.dwg").read_bytes() == b"edited-two"
    assert staged_records[0].source.read_bytes() == b"One.dwg"
    assert staged_records[1].source.read_bytes() == b"Two.dwg"


async def test_restaging_same_source_does_not_overwrite_tab_edits(tmp_path):
    source = tmp_path / "Part.dwg"
    source.write_bytes(b"source")
    staging = StagingManager("alpha", tmp_path / "staging")
    first = staging.stage(source)
    first.staged.write_bytes(b"agent edits")

    second = staging.stage(source)

    assert second is first
    assert len(staging.records) == 1
    assert first.staged.read_bytes() == b"agent edits"


@pytest.mark.parametrize("startup_name", ["Чертеж1.dwg", "Drawing1.dwg"])
async def test_localized_blank_startup_tab_is_closed_after_open(
    monkeypatch, tmp_path, startup_name
):
    monkeypatch.setenv("ISOLATED_INSTANCES", "true")
    backend, app, _placements = _com_backend(monkeypatch)
    startup = _Doc(app, startup_name, "")
    app.Documents.docs.append(startup)
    app.ActiveDocument = startup
    source = tmp_path / "Part.dwg"
    source.write_bytes(b"part")

    result = await backend.drawing_open(str(source))

    assert result["closed_startup_docs"] == [startup_name]
    assert [doc.Name for doc in app.Documents.docs] == ["Part.dwg"]
    assert app.ActiveDocument.FullName == str(source)


async def test_explicitly_created_blank_document_is_not_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("ISOLATED_INSTANCES", "true")
    backend, app, _placements = _com_backend(monkeypatch)
    await backend.drawing_new()
    created = app.ActiveDocument
    source = tmp_path / "Part.dwg"
    source.write_bytes(b"part")

    result = await backend.drawing_open(str(source))

    assert result["closed_startup_docs"] == []
    assert created in app.Documents.docs
    assert app.Documents.Count == 2


async def test_create_with_source_then_open_activates_one_tab(monkeypatch, tmp_path):
    backend, app, _placements = _com_backend(monkeypatch)
    manager = ProjectSessionManager()
    session = ProjectSession(
        "alpha", backend, staging=StagingManager("alpha", tmp_path / "staging")
    )
    manager.sessions["alpha"] = session
    manager.active_project = "alpha"
    source = tmp_path / "Part.dwg"
    source.write_bytes(b"part")
    create_kwargs = {}

    async def fake_create(*args, **kwargs):
        create_kwargs.update(kwargs)
        return session

    monkeypatch.setattr(manager, "create", fake_create)
    ctx = SimpleNamespace(
        lifespan_context={"sessions": manager},
        info=AsyncMock(),
        report_progress=AsyncMock(),
    )

    created = await server.project_session_create(
        "alpha", source_dwg=str(source), ctx=ctx
    )
    reopened = await server.drawing_open(str(source), ctx=ctx)

    assert create_kwargs["startup_source_dwg"] == str(source)
    assert created["opened"]["already_open"] is False
    assert reopened["already_open"] is True
    assert app.Documents.open_calls == [str(source)]
    assert app.Documents.Count == 1


async def test_read_only_open_never_stages_and_uses_native_read_only(
    monkeypatch, tmp_path
):
    backend, app, _placements = _com_backend(monkeypatch)
    manager = ProjectSessionManager()
    staging = StagingManager("alpha", tmp_path / "staging")
    manager.sessions["alpha"] = ProjectSession("alpha", backend, staging=staging)
    manager.active_project = "alpha"
    source = tmp_path / "Part.dwg"
    source.write_bytes(b"part")
    monkeypatch.setattr(config.settings, "staging_mode", "always")
    ctx = SimpleNamespace(
        lifespan_context={"sessions": manager},
        info=AsyncMock(),
        report_progress=AsyncMock(),
    )

    result = await server.drawing_open(str(source), read_only=True, ctx=ctx)

    assert result["read_only"] is True
    assert app.Documents.open_read_only == [True]
    assert staging.records == {}


async def test_attach_existing_never_reopens_moves_or_quits_user_seat(
    monkeypatch, tmp_path
):
    from backends.com_process_guard import ATTACHED_EXISTING_PIDS, process_guard

    process_guard.reset_for_tests()
    backend, app, placements = _com_backend(monkeypatch)
    existing = _Doc(app, "Existing.dwg", str(tmp_path / "Existing.dwg"))
    app.Documents.docs.append(existing)
    app.ActiveDocument = existing
    monkeypatch.setattr(cb, "_find_existing_autocad", lambda *args: app)
    monkeypatch.setattr(
        cb,
        "win32process",
        SimpleNamespace(GetWindowThreadProcessId=lambda _hwnd: (7, 4242)),
    )
    manager = ProjectSessionManager()
    manager.sessions["alpha"] = ProjectSession(
        "alpha", backend, staging=StagingManager("alpha", tmp_path / "staging")
    )
    manager.active_project = "alpha"

    try:
        attached = await backend.attach_existing(target_pid=4242)

        assert attached["already_open"] is True
        assert attached["document"] == "Existing.dwg"
        assert 4242 in ATTACHED_EXISTING_PIDS
        assert backend.document_identity()["name"] == "Existing.dwg"
        assert app.Documents.open_calls == []
        assert placements == []

        closed = await manager.close("alpha", save=False)
        process_guard.close_owned()  # atexit uses the same owned-only cleanup.

        assert closed["window_closed"] is False
        assert app.quit_calls == 0
        assert 4242 not in process_guard._cleanup
    finally:
        process_guard.reset_for_tests()


async def test_absolute_paths_with_the_same_name_open_separate_tabs(
    monkeypatch, tmp_path
):
    backend, app, _placements = _com_backend(monkeypatch)
    first = tmp_path / "one" / "Part.dwg"
    second = tmp_path / "two" / "Part.dwg"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"one")
    second.write_bytes(b"two")

    await backend.drawing_open(str(first))
    await backend.drawing_open(str(second))

    assert app.Documents.open_calls == [str(first), str(second)]
    assert app.Documents.Count == 2
    assert app.ActiveDocument.FullName == str(second)


async def test_explicit_stage_opens_shadow_even_when_source_is_already_open(
    monkeypatch, tmp_path
):
    backend, app, _placements = _com_backend(monkeypatch)
    manager = ProjectSessionManager()
    staging = StagingManager("alpha", tmp_path / "staging")
    manager.sessions["alpha"] = ProjectSession("alpha", backend, staging=staging)
    manager.active_project = "alpha"
    source = tmp_path / "Part.dwg"
    source.write_bytes(b"source")
    monkeypatch.setattr(config.settings, "staging_mode", "off")
    ctx = SimpleNamespace(
        lifespan_context={"sessions": manager},
        info=AsyncMock(),
        report_progress=AsyncMock(),
    )
    await server.drawing_open(str(source), ctx=ctx)

    result = await server.project_stage_dwg("alpha", str(source), ctx=ctx)

    staged_path = result["staging"]["staged_dwg_path"]
    assert result["opened"]["path"] == staged_path
    assert result["opened"]["name"] == "Part.staged.dwg"
    assert app.Documents.open_calls == [str(source), staged_path]
    assert app.ActiveDocument.FullName == staged_path
