"""Regression tests for Windows UNC paths used by CAD file operations."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

import config
from backends import com_backend as cb
from backends.staging import StagingManager, is_locked_by_other_process
from security import normalize_cad_path, validate_path

pytestmark = pytest.mark.skipif(os.name != "nt", reason="UNC paths are a Windows concept")

UNC_DRAWING = r"\\filesrv.ektz.lc\__ОБЪЕКТЫ\Чертеж.dwg"


@pytest.mark.parametrize(
    "raw",
    [
        UNC_DRAWING,
        r"\filesrv.ektz.lc\__ОБЪЕКТЫ\Чертеж.dwg",
        "//filesrv.ektz.lc/__ОБЪЕКТЫ/Чертеж.dwg",
        f'  "{UNC_DRAWING}"  ',
        r"\\?\UNC\filesrv.ektz.lc\__ОБЪЕКТЫ\Чертеж.dwg",
    ],
)
def test_normalize_cad_path_preserves_and_repairs_unc(raw):
    assert normalize_cad_path(raw) == UNC_DRAWING


def test_normalize_cad_path_strips_extended_drive_prefix():
    assert normalize_cad_path(r"\\?\D:\project\plan.dwg") == r"D:\project\plan.dwg"


def test_validate_path_accepts_unc_without_resolution(monkeypatch):
    monkeypatch.setattr(config.settings, "allowed_paths", [])
    monkeypatch.setattr(config.settings, "allow_unc_paths", True)
    path = r"\\filesrv.ektz.lc\__ОБЪЕКТЫ\project\plan.dwg"

    assert str(validate_path(path)) == path


def test_validate_path_unc_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config.settings, "allow_unc_paths", False)

    with pytest.raises(ToolError, match="UNC network paths are disabled"):
        validate_path(UNC_DRAWING)


def test_validate_path_rejects_unc_parent_traversal():
    with pytest.raises(ToolError, match="traversal"):
        validate_path(r"\\filesrv.ektz.lc\__ОБЪЕКТЫ\..\secret.dwg")


def test_allowed_paths_cross_anchor_mismatch_is_clean(monkeypatch):
    monkeypatch.setattr(config.settings, "allowed_paths", [Path(r"D:\local-only")])
    monkeypatch.setattr(config.settings, "allow_unc_paths", True)

    with pytest.raises(ToolError, match="not inside any allowed"):
        validate_path(UNC_DRAWING)


def test_allowed_paths_can_contain_unc_root(monkeypatch):
    root = r"\\filesrv.ektz.lc\__ОБЪЕКТЫ"
    monkeypatch.setattr(config.settings, "allowed_paths", [Path(root)])
    monkeypatch.setattr(config.settings, "allow_unc_paths", True)

    assert str(validate_path(UNC_DRAWING)) == UNC_DRAWING


def test_unc_staging_uses_local_root(monkeypatch, tmp_path):
    source = Path(UNC_DRAWING)
    manager = StagingManager("alpha", tmp_path / "staging")
    monkeypatch.setattr("backends.staging.validate_path", lambda _path: source)
    monkeypatch.setattr("backends.staging.is_locked_by_user", lambda _path: False)
    monkeypatch.setattr(Path, "is_file", lambda self: self == source)

    def fake_copy2(_source, destination):
        Path(destination).write_bytes(b"dwg")
        return destination

    monkeypatch.setattr("backends.staging.shutil.copy2", fake_copy2)
    staged = manager.stage(source)

    assert staged.staged.parent == tmp_path / "staging" / "alpha"
    assert not str(staged.staged).startswith("\\\\")
    assert staged.staged.read_bytes() == b"dwg"


def test_unc_configured_staging_root_falls_back_to_local_app_data(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    manager = StagingManager("alpha", r"\\filesrv.ektz.lc\__ОБЪЕКТЫ\staging")

    assert manager.root == tmp_path / "autocad-mcp" / "staging" / "alpha"


def test_unc_lock_probe_treats_oserror_as_unknown_not_locked(monkeypatch):
    def fail(_self):
        raise OSError("SMB permission denied")

    monkeypatch.setattr(Path, "exists", fail)
    assert is_locked_by_other_process(UNC_DRAWING) is False


class _Doc:
    def __init__(self, app, path):
        self.app = app
        self.FullName = path
        self.Name = os.path.basename(path)
        self.Saved = True
        self.ModelSpace = SimpleNamespace(Count=0)

    def Activate(self):
        self.app.ActiveDocument = self


class _Documents:
    def __init__(self, app):
        self.app = app
        self.docs = []
        self.open_calls = []

    @property
    def Count(self):
        return len(self.docs)

    def Item(self, index):
        return self.docs[index]

    def Open(self, path, read_only=False):
        self.open_calls.append((path, bool(read_only)))
        doc = _Doc(self.app, path)
        self.docs.append(doc)
        self.app.ActiveDocument = doc
        return doc


def _app():
    app = SimpleNamespace(Version="test")
    app.Documents = _Documents(app)
    app.ActiveDocument = None
    return app


def test_open_tab_matching_normalizes_both_unc_spellings(monkeypatch):
    app = _app()
    existing = _Doc(app, r"\\?\UNC\filesrv.ektz.lc\__ОБЪЕКТЫ\Чертеж.dwg")
    app.Documents.docs.append(existing)
    backend = cb.ComBackend("alpha")
    monkeypatch.setattr(cb, "_acad_app", lambda: app)

    assert backend._find_open_tab_for_path(UNC_DRAWING) is existing


@pytest.mark.asyncio
async def test_com_read_only_open_uses_normalized_wide_string_path(monkeypatch):
    app = _app()
    backend = cb.ComBackend("alpha")
    monkeypatch.setattr(cb, "_acad_app", lambda: app)
    monkeypatch.setattr(backend, "_reapply_background_sync", lambda _app: None)

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def no_rebind():
        return None

    monkeypatch.setattr(backend, "_run", run_inline)
    monkeypatch.setattr(backend, "_rebind", no_rebind)

    result = await backend.drawing_open("//filesrv.ektz.lc/__ОБЪЕКТЫ/Чертеж.dwg", read_only=True)

    assert app.Documents.open_calls == [(UNC_DRAWING, True)]
    assert result["path"] == UNC_DRAWING
    assert result["read_only"] is True
