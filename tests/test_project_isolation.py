"""Isolation, ownership, staging, Core Console and screenshot safety tests.

No test in this module starts or attaches to AutoCAD. Win32 and COM boundaries
are faked; the ownership decisions and subprocess protocol are exercised for real.
"""

from __future__ import annotations

import asyncio
import ctypes
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

import backends.com_backend as cb
import backends.com_process_guard as cpg
import backends.com_watchdog as cwd
import backends.coreconsole_runner as cc
import config
from backends.com_backend import ComBackend
from backends.com_process_guard import ProcessGuard, VirtualDesktopManager
from backends.com_watchdog import DialogWatchdog
from backends.coreconsole_runner import CoreConsoleError, CoreConsoleRunner
from backends.staging import StagingError, StagingManager, is_locked_by_user
from security import UserInstanceProtectedError

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def local_process_guard(monkeypatch):
    guard = ProcessGuard()
    monkeypatch.setattr(cpg, "process_guard", guard)
    return guard


def _map_hwnds(monkeypatch, mapping: dict[int, int]) -> None:
    monkeypatch.setattr(cpg, "window_pid", lambda hwnd: mapping[int(hwnd)])


def test_process_guard_refuses_protected_and_unknown_targets(monkeypatch):
    guard = ProcessGuard()
    guard.protect_existing({101})
    _map_hwnds(monkeypatch, {1001: 101, 1002: 102})

    with pytest.raises(UserInstanceProtectedError):
        guard.assert_owned_pid(101)
    with pytest.raises(UserInstanceProtectedError):
        guard.assert_owned_pid(102)
    with pytest.raises(UserInstanceProtectedError):
        guard.assert_owned_hwnd(1001)
    with pytest.raises(UserInstanceProtectedError):
        guard.assert_owned_hwnd(1002)


def test_cleanup_runs_only_for_currently_owned_pids():
    guard = ProcessGuard()
    called = []
    guard.register_owned(201, "one")
    guard.register_owned(202, "two")
    guard.register_cleanup(201, lambda: called.append(201))
    guard.register_cleanup(202, lambda: called.append(202))
    guard.protect_pid(202)

    guard.close_owned()

    assert called == [201]
    with pytest.raises(UserInstanceProtectedError):
        guard.register_cleanup(999, lambda: called.append(999))


def test_virtual_desktop_failure_falls_back_without_activation(monkeypatch, local_process_guard):
    local_process_guard.register_owned(301, "alpha")
    _map_hwnds(monkeypatch, {3001: 301})
    manager = VirtualDesktopManager(local_process_guard)
    move = MagicMock(side_effect=RuntimeError("no virtual desktop API"))
    show = MagicMock()
    set_pos = MagicMock()
    title = MagicMock()
    focus = MagicMock()
    monkeypatch.setattr(manager, "_move_to_virtual_desktop", move)
    monkeypatch.setattr(manager, "_show_no_activate", show)
    monkeypatch.setattr(manager, "_set_pos", set_pos)
    monkeypatch.setattr(manager, "_title", title)
    monkeypatch.setattr(cpg, "win32gui", SimpleNamespace(SetForegroundWindow=focus))

    assert manager.place(3001, "alpha", "virtual_desktop") == "bottom"
    show.assert_called_once_with(3001)
    set_pos.assert_called_once_with(3001, 0, 0, bottom=True)
    focus.assert_not_called()


def test_watchdog_reads_and_closes_only_its_own_project(monkeypatch):
    guard = ProcessGuard()
    guard.protect_existing({401})
    guard.register_owned(402, "alpha")
    guard.register_owned(403, "beta")
    guard.authorize_attached_pid(405, "alpha")
    _map_hwnds(
        monkeypatch, {4001: 401, 4002: 402, 4003: 403, 4004: 404, 4005: 405}
    )
    win32gui = MagicMock()
    win32gui.GetClassName.return_value = "#32770"
    win32gui.GetWindowText.side_effect = lambda hwnd: f"dialog-{hwnd}"
    win32gui.IsWindowVisible.return_value = True

    def enum_windows(callback, _extra):
        for hwnd in (4001, 4002, 4003, 4004, 4005):
            callback(hwnd, None)

    win32gui.EnumWindows.side_effect = enum_windows

    def enum_child_windows(hwnd, callback, _extra):
        assert hwnd == 4002
        callback(4202, None)

    win32gui.EnumChildWindows.side_effect = enum_child_windows
    monkeypatch.setattr(cwd, "win32gui", win32gui)
    monkeypatch.setattr(cwd, "win32con", SimpleNamespace(WM_CLOSE=0x10))
    watchdog = DialogWatchdog("alpha", guard=guard)

    events = watchdog.poll_once()

    assert [event["hwnd"] for event in events] == [4002]
    assert events[0]["project_id"] == "alpha"
    assert events[0]["text"] == "dialog-4202"
    win32gui.PostMessage.assert_called_once_with(4002, 0x10, 0, 0)
    read_hwnds = {call.args[0] for call in win32gui.GetWindowText.call_args_list}
    assert read_hwnds == {4002, 4202}
    assert win32gui.EnumChildWindows.call_count == 1


def test_staging_copies_atomically_and_commits_separate_output(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "staging_mode", "always")
    source = tmp_path / "part.dwg"
    source.write_bytes(b"original-dwg")
    manager = StagingManager("alpha", tmp_path / ".mcp_staging")
    real_replace = __import__("os").replace
    replace = MagicMock(wraps=real_replace)
    monkeypatch.setattr("backends.staging.os.replace", replace)

    staged = manager.stage(source)
    staged.staged.write_bytes(b"agent-edit")
    result = manager.commit()

    assert source.read_bytes() == b"original-dwg"
    assert staged.staged.parent == tmp_path / ".mcp_staging" / "alpha"
    assert result["output_path"] == str(tmp_path / "part_AI_v2.dwg")
    assert (tmp_path / "part_AI_v2.dwg").read_bytes() == b"agent-edit"
    assert replace.call_count == 2
    assert not list(staged.staged.parent.glob("*.tmp.dwg"))


@pytest.mark.parametrize("suffix", [".dwl", ".dwl2"])
def test_staging_detects_autocad_sidecars_and_refuses_source_overwrite(tmp_path, suffix):
    source = tmp_path / "locked.dwg"
    source.write_bytes(b"user-data")
    source.with_suffix(suffix).write_text("user", encoding="utf-8")
    manager = StagingManager("alpha", tmp_path / "staging")
    manager.stage(source)

    assert manager.current.lock_detected is True
    assert is_locked_by_user(source) is True
    with pytest.raises(StagingError, match="without overwrite_if_unlocked"):
        manager.commit(source)
    with pytest.raises(StagingError, match="locked by the user"):
        manager.commit(overwrite_if_unlocked=True)
    assert source.read_bytes() == b"user-data"


def test_staging_refuses_locked_output(tmp_path):
    source = tmp_path / "part.dwg"
    output = tmp_path / "deliverable.dwg"
    source.write_bytes(b"source")
    output.write_bytes(b"old-output")
    output.with_suffix(".dwl2").write_text("user", encoding="utf-8")
    manager = StagingManager("alpha", tmp_path / "staging")
    manager.stage(source)

    with pytest.raises(StagingError, match="locked output"):
        manager.commit(output)
    assert output.read_bytes() == b"old-output"


def test_coreconsole_command_order_and_isolation_user(tmp_path):
    executable = tmp_path / "accoreconsole.exe"
    executable.touch()
    runner = CoreConsoleRunner(executable)

    command = runner.build_command(tmp_path / "input.dwg", tmp_path / "run.scr", "User Name/alpha")

    assert command == [
        str(executable.resolve()),
        "/i",
        str((tmp_path / "input.dwg").resolve()),
        "/s",
        str((tmp_path / "run.scr").resolve()),
        "/l",
        "en-US",
        "/isolate",
        "User_Name_alpha",
    ]


class _FakeProcess:
    def __init__(self, stdout=b"completed", stderr=b"", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.kill = MagicMock()

    async def communicate(self):
        return self.stdout, self.stderr


async def test_coreconsole_executes_without_a_window(tmp_path, monkeypatch):
    drawing = tmp_path / "input.dwg"
    drawing.write_bytes(b"dwg")
    executable = tmp_path / "accoreconsole.exe"
    executable.touch()
    process = _FakeProcess()
    created = AsyncMock(return_value=process)
    monkeypatch.setattr(cc.asyncio, "create_subprocess_exec", created)
    monkeypatch.setattr(cc.getpass, "getuser", lambda: "Test User")

    result = await CoreConsoleRunner(executable).run(
        drawing, "_.AUDIT\n_Y\n", project_id="alpha/1", timeout=5
    )

    args = created.call_args.args
    assert args[0] == str(executable.resolve())
    assert args[1:3] == ("/i", str(drawing.resolve()))
    assert args[3] == "/s"
    assert args[5:7] == ("/l", "en-US")
    assert args[7:] == ("/isolate", "Test_User-alpha_1")
    assert created.call_args.kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert created.call_args.kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert result["returncode"] == 0
    assert result["stdout"] == "completed"
    process.kill.assert_not_called()


async def test_coreconsole_timeout_kills_the_child(tmp_path, monkeypatch):
    drawing = tmp_path / "input.dwg"
    drawing.write_bytes(b"dwg")
    executable = tmp_path / "accoreconsole.exe"
    executable.touch()
    process = _FakeProcess(returncode=-1)
    monkeypatch.setattr(cc.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))

    async def expire(awaitable, *, timeout):
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(cc.asyncio, "wait_for", expire)

    with pytest.raises(CoreConsoleError, match="timed out"):
        await CoreConsoleRunner(executable).run(drawing, "_.AUDIT", timeout=1)
    process.kill.assert_called_once_with()


def _patch_spawn_com(monkeypatch, app, pid):
    dispatch_ex = MagicMock(return_value=app)
    monkeypatch.setattr(
        cb,
        "win32com",
        SimpleNamespace(client=SimpleNamespace(DispatchEx=dispatch_ex)),
        raising=False,
    )
    monkeypatch.setattr(cb, "pythoncom", SimpleNamespace(CLSCTX_LOCAL_SERVER=23), raising=False)
    monkeypatch.setattr(
        cb,
        "win32process",
        SimpleNamespace(GetWindowThreadProcessId=lambda _hwnd: (7, pid)),
        raising=False,
    )
    return dispatch_ex


def test_isolated_spawn_uses_dispatchex_registers_and_places(monkeypatch, local_process_guard):
    monkeypatch.setenv("ISOLATED_INSTANCES", "true")
    app = MagicMock(HWND=5001)
    app.Documents.Count = 1
    dispatch_ex = _patch_spawn_com(monkeypatch, app, 501)
    _map_hwnds(monkeypatch, {5001: 501})
    placements = []

    class Placement:
        def __init__(self, guard):
            self.guard = guard

        def place(self, hwnd, project_id, mode):
            self.guard.assert_owned_hwnd(hwnd)
            placements.append((hwnd, project_id, mode))
            return mode

    monkeypatch.setattr(cpg, "VirtualDesktopManager", Placement)

    assert cb.spawn_isolated_autocad("alpha", "bottom") is app
    dispatch_ex.assert_called_once_with(config.settings.cad_progid, clsctx=23)
    local_process_guard.assert_owned_pid(501)
    assert local_process_guard.project_for_pid(501) == "alpha"
    assert placements == [(5001, "alpha", "bottom")]
    expected = {
        "SDI": 0,
        "TASKBAR": 0,
        "STARTUP": 2,
        "FILEDIA": 0,
        "CMDDIA": 0,
        "ATTDIA": 0,
        "EXPERT": 5,
        "PROXYNOTICE": 0,
        "SECURELOAD": 0,
        "RECOVERYMODE": 0,
    }
    assert {
        call.args[0]: call.args[1] for call in app.ActiveDocument.SetVariable.call_args_list
    } == expected


def test_failed_spawn_closes_only_the_new_owned_process(monkeypatch, local_process_guard):
    app = MagicMock(HWND=6001)
    app.Documents.Count = 0
    _patch_spawn_com(monkeypatch, app, 601)
    _map_hwnds(monkeypatch, {6001: 601})

    class FailPlacement:
        def __init__(self, guard):
            self.guard = guard

        def place(self, hwnd, _project_id, _mode):
            self.guard.assert_owned_hwnd(hwnd)
            raise RuntimeError("placement failed")

    monkeypatch.setattr(cpg, "VirtualDesktopManager", FailPlacement)

    with pytest.raises(RuntimeError, match="placement failed"):
        cb.spawn_isolated_autocad("alpha")
    app.Quit.assert_called_once_with()
    assert local_process_guard.project_for_pid(601) is None


def test_spawn_race_with_protected_pid_never_quits_it(monkeypatch, local_process_guard):
    app = MagicMock(HWND=7001)
    _patch_spawn_com(monkeypatch, app, 701)
    local_process_guard.protect_existing({701})

    with pytest.raises(UserInstanceProtectedError):
        cb.spawn_isolated_autocad("alpha")
    app.Quit.assert_not_called()


def test_isolated_app_never_uses_rot_or_plain_dispatch(monkeypatch):
    monkeypatch.setenv("ISOLATED_INSTANCES", "true")
    app = MagicMock()
    backend = ComBackend("alpha")
    backend._register_spawned_app = MagicMock()
    client = MagicMock()
    client.GetActiveObject.side_effect = AssertionError("ROT attachment is forbidden")
    client.Dispatch.side_effect = AssertionError("plain Dispatch is forbidden")
    monkeypatch.setattr(cb, "win32com", SimpleNamespace(client=client), raising=False)
    monkeypatch.setattr(cb, "spawn_isolated_autocad", lambda *_args: app)
    cb._COM_STATE.clear()
    cb._JOB_STATE.backend = backend
    try:
        assert cb._acad_app() is app
        backend._register_spawned_app.assert_called_once_with(app)
    finally:
        cb._COM_STATE.clear()
        cb._JOB_STATE.backend = None


def test_legacy_attachment_requires_explicit_opt_out(monkeypatch):
    monkeypatch.setenv("ISOLATED_INSTANCES", "false")
    app = MagicMock()
    client = MagicMock()
    client.GetActiveObject.return_value = app
    monkeypatch.setattr(cb, "win32com", SimpleNamespace(client=client), raising=False)
    cb._COM_STATE.clear()
    try:
        assert cb._acad_app() is app
        client.GetActiveObject.assert_called_once_with(config.settings.cad_progid)
        client.DispatchEx.assert_not_called()
    finally:
        cb._COM_STATE.clear()


def _fake_capture(monkeypatch, rendered, bits):
    print_window = MagicMock(return_value=rendered)
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(user32=SimpleNamespace(PrintWindow=print_window)),
        raising=False,
    )
    dc = MagicMock()
    dc.GetSafeHdc.return_value = 88
    mfc = MagicMock()
    mfc.CreateCompatibleDC.return_value = dc
    bitmap = MagicMock()
    bitmap.GetBitmapBits.return_value = bits
    bitmap.GetHandle.return_value = 99
    win32gui = MagicMock()
    win32gui.GetWindowRect.return_value = (0, 0, 2, 1)
    win32gui.GetWindowDC.return_value = 77
    win32ui = MagicMock()
    win32ui.CreateDCFromHandle.return_value = mfc
    win32ui.CreateBitmap.return_value = bitmap
    monkeypatch.setattr(cb, "win32gui", win32gui, raising=False)
    monkeypatch.setattr(cb, "win32ui", win32ui, raising=False)
    monkeypatch.setattr(cb, "PILImage", Image)
    monkeypatch.setattr(cb, "_WIN32_AVAILABLE", True)
    monkeypatch.setattr(cb, "_PIL_OK", True)
    return print_window


@pytest.mark.parametrize(
    ("rendered", "bits", "captured"),
    [
        (1, bytes([0, 0, 0, 255, 255, 255, 255, 255]), True),
        (0, bytes([0, 0, 0, 255, 255, 255, 255, 255]), False),
        (1, bytes(8), False),
    ],
)
def test_printwindow_fullcontent_and_blank_frame_rejection(monkeypatch, rendered, bits, captured):
    print_window = _fake_capture(monkeypatch, rendered, bits)

    image = cb._capture_window(8001)

    print_window.assert_called_once_with(8001, 88, 0x00000002)
    assert (image is not None) is captured


async def test_blank_capture_uses_headless_fallback_for_owned_window(
    monkeypatch, local_process_guard
):
    monkeypatch.setenv("ISOLATED_INSTANCES", "true")
    local_process_guard.register_owned(901, "alpha")
    _map_hwnds(monkeypatch, {9001: 901})
    app = MagicMock(HWND=9001)
    app.Documents.Count = 1
    backend = ComBackend("alpha")
    backend._bound = {"document_id": "bound"}
    backend._guard = MagicMock()

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    fallback = MagicMock(return_value=b"headless-png")

    async def headless():
        return fallback()

    monkeypatch.setattr(backend, "_run", run_inline)
    monkeypatch.setattr(backend, "_headless_screenshot_fallback", headless)
    monkeypatch.setattr(cb, "_acad_app", lambda: app)
    monkeypatch.setattr(cb, "_capture_window", lambda _hwnd: None)

    assert await backend.view_screenshot() == b"headless-png"
    fallback.assert_called_once_with()


async def test_screenshot_refuses_user_owned_hwnd(monkeypatch, local_process_guard):
    monkeypatch.setenv("ISOLATED_INSTANCES", "true")
    local_process_guard.protect_existing({1001})
    _map_hwnds(monkeypatch, {10001: 1001})
    app = MagicMock(HWND=10001)
    app.Documents.Count = 0
    backend = ComBackend("alpha")

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    capture = MagicMock()
    monkeypatch.setattr(backend, "_run", run_inline)
    monkeypatch.setattr(cb, "_acad_app", lambda: app)
    monkeypatch.setattr(cb, "_capture_window", capture)

    with pytest.raises(UserInstanceProtectedError):
        await backend.view_screenshot()
    capture.assert_not_called()


def test_hwnd_enumeration_skips_unknown_and_other_projects(monkeypatch, local_process_guard):
    monkeypatch.setenv("ISOLATED_INSTANCES", "true")
    local_process_guard.register_owned(1102, "beta")
    local_process_guard.register_owned(1103, "alpha")
    _map_hwnds(monkeypatch, {11001: 1101, 11002: 1102, 11003: 1103})
    app = MagicMock()
    type(app).HWND = property(lambda _self: (_ for _ in ()).throw(RuntimeError("no HWND")))
    win32gui = MagicMock()
    win32gui.GetWindowText.return_value = "AutoCAD"
    win32gui.IsWindowVisible.return_value = True

    def enum_windows(callback, _extra):
        for hwnd in (11001, 11002, 11003):
            if callback(hwnd, None) is False:
                break

    win32gui.EnumWindows.side_effect = enum_windows
    monkeypatch.setattr(cb, "_acad_app", lambda: app)
    monkeypatch.setattr(cb, "win32gui", win32gui, raising=False)
    monkeypatch.setattr(cb, "_WIN32_AVAILABLE", True)

    assert cb._find_autocad_hwnd("alpha") == 11003


async def test_isolated_timeout_disables_backend_without_second_executor(monkeypatch):
    monkeypatch.setenv("ISOLATED_INSTANCES", "true")
    monkeypatch.setattr(config.settings, "com_call_timeout", 0.05)
    backend = ComBackend("alpha")
    backend._executor = ThreadPoolExecutor(max_workers=1)
    backend._connected = True
    backend._pid = 1201
    replacement = MagicMock()
    monkeypatch.setattr(backend, "_new_executor", replacement)
    release = threading.Event()
    finished = threading.Event()

    def hang():
        try:
            release.wait(5)
        finally:
            finished.set()

    try:
        with pytest.raises(RuntimeError, match="Close this isolated project session"):
            await backend._run(hang)
        assert backend._executor is None
        assert backend.pid == 1201
        replacement.assert_not_called()
        with pytest.raises(RuntimeError, match="not connected"):
            await backend._run(lambda: None)
    finally:
        release.set()
        assert finished.wait(5)
