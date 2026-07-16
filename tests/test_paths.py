from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.paths as paths_module
from src.paths import bundle_dir, is_frozen, user_data_dir


def test_not_frozen_by_default():
    assert is_frozen() is False


def test_bundle_dir_is_repo_root_when_not_frozen():
    root = bundle_dir()
    assert (root / "templates" / "index.html").exists()
    assert (root / "app.py").exists()


def test_user_data_dir_is_repo_data_when_not_frozen():
    assert user_data_dir() == Path(__file__).parent.parent / "data"


def test_user_data_dir_is_per_user_and_platform_specific_when_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(paths_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths_module.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    result = user_data_dir()

    assert result == tmp_path / "CityArtsTeacherSearch"
    assert result.is_dir()  # created on demand


def test_bundle_dir_uses_meipass_when_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(paths_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths_module.sys, "_MEIPASS", str(tmp_path), raising=False)

    assert bundle_dir() == tmp_path
