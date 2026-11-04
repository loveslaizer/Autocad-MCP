"""OpenCode Desktop lifecycle and process-ownership shutdown tests."""

from __future__ import annotations

import concurrent.futures
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import backends.com_backend as cb
import backends.com_process_guard as cpg
import config
import server
from backends.com_process_guard import ProcessEntry, ProcessGuard
from backends.project_sessions import ProjectSession, ProjectSessionManager


@pytest.mark.parametrize(
    ("env_name", "attribute"),
    [
        ("WATCH_OPENCODE_DESKTOP", "watch_opencode_desktop"),
        ("STOP_OPENCODE_DAEMON_ON_EXIT", "stop_opencode_daemon_on_exit"),
        ("CLOSE_AGENT_CAD_ON_EXIT", "close_agent_cad_on_exit"),
    ],
)
def test_opencode_lifecycle_settings_default_true_and_accept_false(
    monkeypatch, env_name, attribute
):
    monkeypatch.delenv(env_name, raising=False)
    assert getattr(config.Settings(), attribute) is True
    monkeypatch.setenv(env_name, "false")
    assert getattr(config.Settings(), attribute) is False


def test_process_snapshot_detects_opencode_case_insensitively(monkeypatch):
    processes = [
        ProcessEntry(pid=10, parent_pid=1, name="OpenCode.EXE"),
        ProcessEntry(pid=20, parent_pid=10, name="opencode-cli.exe"),
    ]
    monkeypatch.setattr(cpg, "running_processes", lambda: processes)

    assert cpg.running_process_named("OpenCode.exe") is True
    assert cpg.running_process_named("OPENCODE-CLI.EXE") is True
    assert cpg.running_process_named("missing.exe") is False
    assert cpg.process_name(20) == "opencode-cli.exe"
    assert cpg.process_name(999) is None


class _ImmediateStopEvent:
    """Let the watchdog run without real 1.5/2-second test delays."""

    def __init__(self):
        self.waits = []

    def is_set(self):
        return False

    def wait(self, timeout):
        self.waits.append(timeout)
        return False


class _InlineExecutor:
    """Run COM jobs synchronously and return real concurrent futures."""

    def submit(self, function, *args):
        future = concurrent.futures.Future()
        try:
            future.set_result(function(*args))
        except BaseException as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, wait=False):
        return None


class _OwnedApp:
    def __init__(self):
        self.HWND = 7401
        self.Documents = SimpleNamespace(Count=1)
        self.ActiveDocument = MagicMock()
        self.quit_calls = 0

        def close(save):
            self.Documents.Count = 0

        self.close_active = close

    def Quit(self):
        self.quit_calls += 1


class _AttachedBackend:
    name = "com"
    is_attached_existing = True
    is_connected = True
    pid = 7402

    def __init__(self):
        self.app = MagicMock()
        self.disconnects = 0

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False


def test_gui_exit_closes_owned_cad_spares_attached_and_stops_daemon(monkeypatch):
    """The transition test uses the real ComBackend owned-process cleanup."""
    guard = ProcessGuard()
    guard.register_owned(7401, "owned")
    monkeypatch.setattr(cpg, "process_guard", guard)
    monkeypatch.setattr(cpg, "window_pid", lambda hwnd: {7401: 7401}[int(hwnd)])
    monkeypatch.setenv("ISOLATED_INSTANCES", "true")
    monkeypatch.setattr(config.settings, "close_agent_cad_on_exit", True)
    monkeypatch.setattr(config.settings, "stop_opencode_daemon_on_exit", True)
    monkeypatch.setattr(config.settings, "shutdown_grace", 0.1)

    app = _OwnedApp()
    app.ActiveDocument.Close.side_effect = app.close_active
    monkeypatch.setitem(cb._COM_STATE, "app", app)
    owned = cb.ComBackend("owned")
    owned._pid = 7401
    owned._hwnd = 7401
    owned._executor = _InlineExecutor()
    owned._connected = True

    attached = _AttachedBackend()
    manager = ProjectSessionManager()
    manager.sessions = {
        "owned": ProjectSession("owned", owned),
        "attached": ProjectSession("attached", attached),
    }
    monkeypatch.setattr(server, "_active_sessions", manager)
    monkeypatch.setattr(server, "_server_loop", None)
    shutdown = MagicMock(wraps=server.shutdown_all_sessions)
    monkeypatch.setattr(server, "shutdown_all_sessions", shutdown)
    monkeypatch.setattr(server, "_opencode_desktop_running", MagicMock(side_effect=[False, False]))
    monkeypatch.setattr(server, "_process_name", lambda pid: "opencode-cli.exe")
    terminate = MagicMock(return_value=True)
    monkeypatch.setattr(server, "_terminate_process", terminate)
    exit_process = MagicMock()
    monkeypatch.setattr(server.os, "_exit", exit_process)
    stop_event = _ImmediateStopEvent()

    server._run_opencode_desktop_watchdog(
        ppid=8800,
        parent_name="opencode-cli.exe",
        initially_running=True,
        stop_event=stop_event,
    )

    shutdown.assert_called_once_with(timeout_sec=3.0)
    app.ActiveDocument.Close.assert_called_once_with(False)
    assert app.quit_calls == 1
    assert attached.disconnects == 1
    attached.app.Quit.assert_not_called()
    attached.app.ActiveDocument.Close.assert_not_called()
    terminate.assert_called_once_with(8800)
    exit_process.assert_called_once_with(0)
    assert stop_event.waits == [1.5]


def test_opencode_restart_recheck_prevents_shutdown(monkeypatch):
    monkeypatch.setattr(server, "_opencode_desktop_running", MagicMock(side_effect=[False, True]))
    exit_process = MagicMock()
    monkeypatch.setattr(server.os, "_exit", exit_process)
    shutdown = MagicMock()
    monkeypatch.setattr(server, "shutdown_all_sessions", shutdown)

    class StopAfterRecheck(_ImmediateStopEvent):
        def wait(self, timeout):
            self.waits.append(timeout)
            return timeout == 2.0

    server._run_opencode_desktop_watchdog(
        ppid=8800,
        parent_name="bun.exe",
        initially_running=True,
        stop_event=StopAfterRecheck(),
    )

    shutdown.assert_not_called()
    exit_process.assert_not_called()
