"""Process ownership and safe window placement for isolated AutoCAD seats.

The registry is deliberately process-wide: every project session and the dialog
watchdog must make the same ownership decision. A PID is either protected (it
existed before an agent spawn) or explicitly owned by this server; unknown PIDs
are refused rather than optimistically treated as safe.
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable

from security import UserInstanceProtectedError

log = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"
if _IS_WINDOWS:
    try:
        import win32con
        import win32gui
        import win32process
    except ImportError:  # pragma: no cover - exercised on installations without [com]
        win32con = win32gui = win32process = None
else:
    win32con = win32gui = win32process = None


def window_pid(hwnd: int) -> int:
    """Return the owning PID, refusing a handle that cannot be identified."""
    if win32process is None:
        raise UserInstanceProtectedError(
            "Cannot verify the AutoCAD window's process: pywin32 is unavailable"
        )
    try:
        pid = int(win32process.GetWindowThreadProcessId(int(hwnd))[1])
    except Exception as exc:
        raise UserInstanceProtectedError(
            f"Cannot verify the process owning HWND {hwnd}: {exc}"
        ) from exc
    if pid <= 0:
        raise UserInstanceProtectedError(f"Cannot verify the process owning HWND {hwnd}")
    return pid


def running_autocad_pids() -> set[int]:
    """Enumerate acad.exe through Toolhelp, including processes without a window."""
    if not _IS_WINDOWS:
        return set()
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot == ctypes.c_void_p(-1).value:
        raise OSError("CreateToolhelp32Snapshot failed while protecting AutoCAD processes")

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", ctypes.c_ulong),
            ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    found: set[int] = set()
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == "acad.exe":
                found.add(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return found


class ProcessGuard:
    """Allowlist of server-spawned AutoCAD processes and their cleanup hooks."""

    def __init__(self):
        self.protected_user_pids: set[int] = set()
        self.agent_owned_pids: set[int] = set()
        self.attached_existing_pids: set[int] = set()
        self._projects: dict[int, str] = {}
        self._hwnds: dict[int, int] = {}
        self._cleanup: dict[int, Callable[[], None]] = {}
        self._lock = threading.RLock()

    def protect_existing(self, pids: set[int] | None = None) -> set[int]:
        """Snapshot every pre-existing AutoCAD PID before a new spawn."""
        discovered = running_autocad_pids() if pids is None else {int(pid) for pid in pids}
        with self._lock:
            self.protected_user_pids.update(discovered - self.agent_owned_pids)
            return set(self.protected_user_pids)

    def protect_pid(self, pid: int) -> None:
        with self._lock:
            self.agent_owned_pids.discard(int(pid))
            self.protected_user_pids.add(int(pid))

    def authorize_attached_pid(
        self, pid: int, project_id: str | None = None, hwnd: int | None = None
    ) -> None:
        """Explicitly authorise COM access without acquiring process ownership."""
        pid = int(pid)
        if pid <= 0:
            raise ValueError("Attached AutoCAD PID must be positive")
        with self._lock:
            self.attached_existing_pids.add(pid)
            if project_id is not None:
                self._projects[pid] = str(project_id)
            if hwnd is not None:
                self._hwnds[pid] = int(hwnd)
            # No cleanup callback is ever registered for an attached process.

    def register_owned(self, pid: int, project_id: str, hwnd: int | None = None) -> None:
        pid = int(pid)
        with self._lock:
            if pid in self.protected_user_pids:
                raise UserInstanceProtectedError(
                    f"AutoCAD PID {pid} existed before the agent spawn and is protected"
                )
            self.agent_owned_pids.add(pid)
            self._projects[pid] = str(project_id)
            if hwnd is not None:
                self._hwnds[pid] = int(hwnd)

    def assert_owned_pid(self, pid: int) -> None:
        pid = int(pid)
        with self._lock:
            if pid in self.attached_existing_pids:
                return
            if pid in self.protected_user_pids or pid not in self.agent_owned_pids:
                raise UserInstanceProtectedError(
                    f"Refusing AutoCAD operation for PID {pid}: it is not an agent-owned isolated process"
                )

    def assert_owned_hwnd(self, hwnd: int) -> int:
        pid = window_pid(hwnd)
        self.assert_owned_pid(pid)
        return pid

    def assert_owned_app(self, app) -> int:
        try:
            hwnd = int(app.HWND)
        except Exception as exc:
            raise UserInstanceProtectedError(
                "Refusing an unverifiable COM Application: Application.HWND could not be read"
            ) from exc
        return self.assert_owned_hwnd(hwnd)

    def project_for_pid(self, pid: int) -> str | None:
        with self._lock:
            return self._projects.get(int(pid))

    def hwnd_for_pid(self, pid: int) -> int | None:
        with self._lock:
            return self._hwnds.get(int(pid))

    def register_cleanup(self, pid: int, callback: Callable[[], None]) -> None:
        self.assert_owned_pid(pid)
        with self._lock:
            if int(pid) in self.attached_existing_pids:
                raise UserInstanceProtectedError(
                    f"Attached AutoCAD PID {pid} must never have a cleanup/Quit callback"
                )
            self._cleanup[int(pid)] = callback

    def unregister(self, pid: int) -> None:
        with self._lock:
            pid = int(pid)
            self.agent_owned_pids.discard(pid)
            self.attached_existing_pids.discard(pid)
            self._projects.pop(pid, None)
            self._hwnds.pop(pid, None)
            self._cleanup.pop(pid, None)

    def close_owned(self) -> None:
        """Run only callbacks registered against owned PIDs; never enumerate-and-kill."""
        with self._lock:
            callbacks = [
                (pid, self._cleanup[pid])
                for pid in tuple(self.agent_owned_pids)
                if pid in self._cleanup and pid not in self.protected_user_pids
            ]
        for pid, callback in callbacks:
            try:
                callback()
            except Exception as exc:  # pragma: no cover - defensive at interpreter exit
                log.warning("Cleanup of agent-owned AutoCAD PID %s failed: %s", pid, exc)

    def reset_for_tests(self) -> None:
        with self._lock:
            self.protected_user_pids.clear()
            self.agent_owned_pids.clear()
            self.attached_existing_pids.clear()
            self._projects.clear()
            self._hwnds.clear()
            self._cleanup.clear()


process_guard = ProcessGuard()
# Backwards-compatible, explicit names for watchdog and integration code.
PROTECTED_USER_PIDS = process_guard.protected_user_pids
AGENT_OWNED_PIDS = process_guard.agent_owned_pids
ATTACHED_EXISTING_PIDS = process_guard.attached_existing_pids
InstanceRegistry = ProcessGuard


def _exit_cleanup() -> None:
    process_guard.close_owned()


atexit.register(_exit_cleanup)
if threading.current_thread() is threading.main_thread():
    for _signal in (signal.SIGINT, signal.SIGTERM):
        try:
            _previous = signal.getsignal(_signal)

            def _handler(signum, frame, previous=_previous):
                _exit_cleanup()
                if callable(previous):
                    previous(signum, frame)

            signal.signal(_signal, _handler)
        except (ValueError, OSError):  # pragma: no cover - embedded runtimes
            pass


class VirtualDesktopManager:
    """Move an owned window without activating it or changing the user's desktop."""

    def __init__(self, guard: ProcessGuard = process_guard):
        self.guard = guard

    def place(self, hwnd: int, project_id: str, mode: str) -> str:
        self.guard.assert_owned_hwnd(hwnd)
        if mode not in {"virtual_desktop", "bottom", "offscreen", "hidden"}:
            raise ValueError(f"Unknown background window mode: {mode}")
        self._title(hwnd, project_id)
        if mode == "virtual_desktop":
            try:
                self._move_to_virtual_desktop(hwnd)
                return "virtual_desktop"
            except Exception as exc:
                log.warning("Virtual Desktop placement failed; using bottom mode: %s", exc)
                mode = "bottom"
        if mode == "bottom":
            self._show_no_activate(hwnd)
            self._set_pos(hwnd, 0, 0, bottom=True)
        elif mode == "offscreen":
            self._show_no_activate(hwnd)
            self._set_pos(hwnd, -32000, -32000)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        return mode

    @staticmethod
    def _title(hwnd: int, project_id: str) -> None:
        title = f"[MCP-AGENT: {project_id}] AutoCAD"
        if win32gui is not None:
            win32gui.SetWindowText(hwnd, title)
        else:  # pragma: no cover - pywin32 is the production route
            ctypes.windll.user32.SetWindowTextW(int(hwnd), title)

    @staticmethod
    def _move_to_virtual_desktop(hwnd: int) -> None:
        from pyvda import AppView, VirtualDesktop

        desktops = list(VirtualDesktop.desktops())
        name = os.environ.get("VIRTUAL_DESKTOP_NAME", "AI CAD Workspace")
        target = next((d for d in desktops if getattr(d, "name", None) == name), None)
        if target is None:
            target = VirtualDesktop.create()
            try:
                target.rename(name)
            except (AttributeError, NotImplementedError):
                pass
        AppView(hwnd=hwnd).move(target)

    @staticmethod
    def _show_no_activate(hwnd: int) -> None:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)

    @staticmethod
    def _set_pos(hwnd: int, x: int, y: int, bottom: bool = False) -> None:
        flags = win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE
        if bottom:
            flags |= win32con.SWP_NOMOVE
            win32gui.SetWindowPos(hwnd, win32con.HWND_BOTTOM, 0, 0, 0, 0, flags)
        else:
            win32gui.SetWindowPos(hwnd, 0, x, y, 0, 0, flags)
