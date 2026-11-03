"""Async, no-window AutoCAD Core Console script runner."""

from __future__ import annotations

import asyncio
import getpass
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import config
from security import validate_path


class CoreConsoleError(RuntimeError):
    """Core Console discovery or execution failed."""


def discover_coreconsole() -> Path:
    """Find accoreconsole.exe from configuration, AutoCAD registry, or acad.exe."""
    configured = config.settings.acad_coreconsole_path
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if candidate.is_file() and candidate.name.lower() == "accoreconsole.exe":
            return candidate
        raise CoreConsoleError(
            f"ACAD_CORECONSOLE_PATH does not name accoreconsole.exe: {candidate}"
        )
    if sys.platform != "win32":
        raise CoreConsoleError("AutoCAD Core Console is available only on Windows")

    import winreg

    roots: list[Path] = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key_path in (r"SOFTWARE\Autodesk\AutoCAD", r"SOFTWARE\Autodesk\AutoCAD\CurVer"):
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    for value_name in ("AcadLocation", "InstallDir"):
                        try:
                            value = winreg.QueryValueEx(key, value_name)[0]
                            if value:
                                roots.append(Path(value))
                        except OSError:
                            pass
                    if key_path.endswith("CurVer"):
                        version = winreg.QueryValueEx(key, "CurVer")[0]
                        roots.extend(_registry_install_roots(hive, version))
            except OSError:
                pass
    for env_name in ("ACAD_LOCATION", "ACAD_INSTALL_DIR"):
        if os.environ.get(env_name):
            roots.append(Path(os.environ[env_name]))
    for root in roots:
        candidate = root / "accoreconsole.exe"
        if candidate.is_file():
            return candidate.resolve()
    raise CoreConsoleError(
        "accoreconsole.exe was not found. Set ACAD_CORECONSOLE_PATH to its full path."
    )


def _registry_install_roots(hive, version: str) -> list[Path]:
    import winreg

    roots = []
    for prefix in (r"SOFTWARE\Autodesk\AutoCAD",):
        try:
            with winreg.OpenKey(hive, f"{prefix}\\{version}") as key:
                value = winreg.QueryValueEx(key, "AcadLocation")[0]
                roots.append(Path(value))
        except OSError:
            pass
    return roots


class CoreConsoleRunner:
    """Build and execute one isolated Core Console invocation per call."""

    def __init__(self, executable: str | Path | None = None):
        self.executable = Path(executable).resolve() if executable else None

    def resolve_executable(self) -> Path:
        return self.executable or discover_coreconsole()

    def build_command(
        self, dwg_path: str | Path, script_path: str | Path, userid: str
    ) -> list[str]:
        user = re.sub(r"[^A-Za-z0-9_.-]", "_", userid)[:80] or "mcp-agent"
        return [
            str(self.resolve_executable()),
            "/i",
            str(Path(dwg_path).resolve()),
            "/s",
            str(Path(script_path).resolve()),
            "/l",
            "en-US",
            "/isolate",
            user,
        ]

    async def run(
        self,
        dwg_path: str | Path,
        script_or_lisp: str,
        *,
        project_id: str = "default",
        timeout: int = 120,
    ) -> dict:
        source = validate_path(str(dwg_path))
        if source.suffix.lower() != ".dwg" or not source.is_file():
            raise CoreConsoleError(f"Core Console needs an existing .dwg input: {source}")
        if not script_or_lisp or not script_or_lisp.strip():
            raise CoreConsoleError("script_or_lisp cannot be empty")
        timeout = max(1, min(int(timeout), 3600))
        safe_project_id = re.sub(r"[^A-Za-z0-9_.-]", "_", project_id)[:80] or "default"
        with tempfile.TemporaryDirectory(prefix=f"acadmcp_core_{safe_project_id}_") as folder:
            script = Path(folder) / "command.scr"
            script.write_text(script_or_lisp.rstrip() + "\n", encoding="utf-8")
            command = self.build_command(source, script, f"{getpass.getuser()}-{project_id}")
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                creationflags=creationflags,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except TimeoutError as exc:
                process.kill()
                stdout, stderr = await process.communicate()
                raise CoreConsoleError(f"Core Console timed out after {timeout}s") from exc
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise CoreConsoleError(
                f"Core Console exited with code {process.returncode}: {err or out[-2000:]}"
            )
        return {
            "ok": True,
            "project_id": project_id,
            "dwg_path": str(source),
            "returncode": process.returncode,
            "stdout": out,
            "stderr": err,
            "command": command,
        }
