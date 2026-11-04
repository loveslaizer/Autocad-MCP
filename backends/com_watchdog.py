"""Modal-dialog watchdog restricted to explicitly agent-owned AutoCAD PIDs."""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections import deque

from .com_process_guard import ProcessGuard, process_guard

log = logging.getLogger(__name__)

if sys.platform == "win32":
    try:
        import win32con
        import win32gui
    except ImportError:  # pragma: no cover
        win32con = win32gui = None
else:
    win32con = win32gui = None


class DialogWatchdog:
    """Poll and dismiss only owned ``#32770`` dialogs, without changing focus."""

    def __init__(
        self,
        project_id: str = "default",
        guard: ProcessGuard = process_guard,
        interval: float = 1.5,
        max_events: int = 200,
    ):
        self.project_id = project_id
        self.guard = guard
        self.interval = max(0.1, float(interval))
        self.watchdog_events: deque[dict] = deque(maxlen=max_events)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"acadmcp-dialog-{self.project_id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=min(2.0, self.interval + 0.2))
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.poll_once()
            except Exception as exc:  # pragma: no cover - watchdog must never stop silently
                log.warning("Dialog watchdog poll failed for %s: %s", self.project_id, exc)

    def poll_once(self) -> list[dict]:
        if win32gui is None:
            return []
        dialogs: list[int] = []

        def collect(hwnd, _):
            try:
                if win32gui.GetClassName(hwnd) == "#32770":
                    dialogs.append(hwnd)
            except Exception:
                pass
            return True

        win32gui.EnumWindows(collect, None)
        handled = []
        for hwnd in dialogs:
            try:
                pid = self.guard.assert_owned_hwnd(hwnd)
            except Exception:
                # Protected and unknown windows are intentionally indistinguishable
                # here: neither is read, messaged, nor even logged as agent work.
                continue
            if pid in self.guard.attached_existing_pids:
                # COM attach permission is not dialog ownership.
                continue
            if self.guard.project_for_pid(pid) != self.project_id:
                # Ownership alone is insufficient: project A must never read or
                # dismiss a modal dialog belonging to project B.
                continue
            event = {
                "project_id": self.project_id,
                "pid": pid,
                "hwnd": int(hwnd),
                "title": win32gui.GetWindowText(hwnd),
                "text": self._dialog_text(hwnd),
                "action": "WM_CLOSE",
                "timestamp": time.time(),
            }
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            self.watchdog_events.append(event)
            handled.append(event)
        return handled

    def project_for_pid(self, pid: int) -> str:
        return self.guard.project_for_pid(pid) or self.project_id

    @staticmethod
    def _dialog_text(hwnd: int) -> str:
        parts: list[str] = []

        def child(child_hwnd, _):
            try:
                text = win32gui.GetWindowText(child_hwnd).strip()
                if text:
                    parts.append(text)
            except Exception:
                pass
            return True

        win32gui.EnumChildWindows(hwnd, child, None)
        return "\n".join(parts)
