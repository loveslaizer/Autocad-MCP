"""Per-project shadow copies of user DWG files with lock-safe publication."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import config
from security import normalize_cad_path, validate_path


class StagingError(RuntimeError):
    """A staging or commit safety check failed."""


@dataclass(frozen=True)
class StagedDrawing:
    project_id: str
    source: Path
    staged: Path
    lock_detected: bool

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "source_dwg_path": str(self.source),
            "staged_dwg_path": str(self.staged),
            "lock_detected": self.lock_detected,
            "staged": True,
        }


def _safe_project_id(project_id: str) -> str:
    value = str(project_id).strip()
    if not value or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value) or value in {".", ".."}:
        raise StagingError("project_id must be 1-80 letters, digits, dots, underscores or hyphens")
    return value


def is_locked_by_other_process(path: str | Path) -> bool:
    """Detect locks without letting SMB permission quirks abort drawing_open."""
    source = Path(normalize_cad_path(str(path)))
    try:
        if not source.exists():
            return False
        sidecars = (
            source.with_suffix(".dwl"),
            source.with_suffix(".dwl2"),
            source.with_name(source.name + ".dwl"),
            source.with_name(source.name + ".dwl2"),
        )
        if any(candidate.exists() for candidate in sidecars):
            return True
    except OSError:
        # An inaccessible sidecar is not evidence that the drawing is locked.
        # Read-only opens must still be allowed to proceed over SMB.
        return False
    if os.name != "nt":
        return False
    try:
        # Windows sharing flags are enforced when Python opens an existing file.
        # A read/write open without truncation detects the usual AutoCAD lock.
        with source.open("r+b"):
            return False
    except PermissionError:
        return True
    except OSError:
        return False


# Backwards-compatible name used by existing integrations and tests.
is_locked_by_user = is_locked_by_other_process


def _local_staging_base(configured: str | Path) -> Path:
    """Return a local staging base, never a directory on a UNC share."""
    base = Path(normalize_cad_path(str(configured)))
    if not base.is_absolute():
        base = Path.cwd() / base
    base = Path(normalize_cad_path(str(base)))
    if str(base).startswith("\\\\"):
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data and not normalize_cad_path(local_app_data).startswith("\\\\"):
            base = Path(local_app_data) / "autocad-mcp" / "staging"
        else:
            base = Path.home() / ".autocad-mcp" / "staging"
    if str(base).startswith("\\\\"):
        raise StagingError("A local staging directory is required for UNC source drawings")
    return Path(normalize_cad_path(str(base.resolve())))


class StagingManager:
    """Own every source/staged pair opened in a project's drawing tabs."""

    def __init__(self, project_id: str, root: str | Path | None = None):
        self.project_id = _safe_project_id(project_id)
        configured = root if root is not None else config.settings.staging_dir
        self.root = _local_staging_base(configured) / self.project_id
        self.records: dict[str, StagedDrawing] = {}
        self.current: StagedDrawing | None = None

    @staticmethod
    def _path_key(path: str | Path) -> str:
        normalized = normalize_cad_path(str(path))
        absolute = normalized if normalized.startswith("\\\\") else os.path.abspath(normalized)
        return os.path.normcase(absolute)

    def find(self, target_dwg: str | Path) -> StagedDrawing | None:
        """Find a record by source/staged path or either file's base name."""
        wanted = str(target_dwg).strip()
        if not wanted:
            return None
        try:
            path_key = self._path_key(wanted)
        except (OSError, ValueError):
            path_key = ""
        path_hits = [
            record
            for record in self.records.values()
            if path_key
            and path_key
            in {self._path_key(record.source), self._path_key(record.staged)}
        ]
        lowered = wanted.lower()
        name_hits = [
            record
            for record in self.records.values()
            if lowered in {record.source.name.lower(), record.staged.name.lower()}
        ]
        hits = path_hits or name_hits
        if len(hits) > 1:
            names = sorted(str(record.source) for record in hits)
            raise StagingError(f"Staged drawing {wanted!r} is ambiguous: {names}")
        return hits[0] if hits else None

    def require_current(self, target_dwg: str | Path | None = None) -> StagedDrawing:
        if target_dwg is not None:
            selected = self.find(target_dwg)
            if selected is None:
                raise StagingError(
                    f"Project {self.project_id!r} has no staged DWG matching {target_dwg!r}"
                )
            return selected
        if self.current is None:
            raise StagingError(f"Project {self.project_id!r} has no active staged DWG")
        return self.current

    def activate_document(self, name_or_path: str | Path | None) -> StagedDrawing | None:
        """Sync the active staged record with the active AutoCAD tab."""
        self.current = self.find(name_or_path) if name_or_path else None
        return self.current

    def stage(self, source_dwg_path: str | Path, *, force: bool = False) -> StagedDrawing:
        source = validate_path(str(source_dwg_path))
        if source.suffix.lower() != ".dwg" or not source.is_file():
            raise StagingError(f"project_stage_dwg needs an existing .dwg file: {source}")
        locked = is_locked_by_user(source)
        if not force and config.settings.staging_mode == "off":
            raise StagingError("STAGING_MODE=off; staging was explicitly requested and is refused")
        source_key = self._path_key(source)
        existing = next(
            (
                record
                for record in self.records.values()
                if self._path_key(record.source) == source_key
            ),
            None,
        )
        if existing is not None:
            # Re-staging an open tab must not overwrite its unsaved agent edits.
            self.current = existing
            return existing
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{source.stem}.staged.dwg"
        target_key = self._path_key(target)
        collision = self.records.get(target_key)
        if collision is not None and self._path_key(collision.source) != source_key:
            raise StagingError(
                f"Staging name collision: {source.name} and {collision.source} both map to {target}"
            )
        temporary = self.root / f".{source.stem}.{os.getpid()}.tmp.dwg"
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        # XREFs remain resolvable relative to the original folder. AutoCAD's
        # project search path is set by the COM session to source.parent; files
        # themselves are never rewritten just to repoint references.
        self.current = StagedDrawing(self.project_id, source, target, locked)
        self.records[target_key] = self.current
        return self.current

    def default_output(self, current: StagedDrawing | None = None) -> Path:
        selected = current or self.require_current()
        return selected.source.with_name(f"{selected.source.stem}_AI_v2.dwg")

    def commit(
        self,
        output_path: str | Path | None = None,
        overwrite_if_unlocked: bool = False,
        target_dwg: str | Path | None = None,
    ) -> dict:
        current = self.require_current(target_dwg)
        if not current.staged.is_file():
            raise StagingError(f"Staged DWG no longer exists: {current.staged}")
        overwrite = output_path is None and overwrite_if_unlocked
        target = (
            current.source
            if overwrite
            else Path(output_path)
            if output_path
            else self.default_output(current)
        )
        target = validate_path(str(target), allow_write=True)
        if target.suffix.lower() != ".dwg":
            raise StagingError("Staged output must be a .dwg file")
        if target == current.source:
            if not overwrite_if_unlocked:
                raise StagingError(
                    "Refusing to overwrite the source without overwrite_if_unlocked=True"
                )
            if is_locked_by_user(current.source):
                raise StagingError("Refusing to overwrite the source: it is locked by the user")
        elif target.exists() and is_locked_by_user(target):
            raise StagingError(f"Refusing to overwrite locked output: {target}")
        temporary = target.with_name(f".{target.stem}.{os.getpid()}.tmp.dwg")
        try:
            shutil.copy2(current.staged, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "ok": True,
            "project_id": self.project_id,
            "source_dwg_path": str(current.source),
            "staged_dwg_path": str(current.staged),
            "output_path": str(target),
            "overwrote_source": target == current.source,
        }
