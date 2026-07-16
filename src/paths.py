"""Path resolution that works both from source and from a PyInstaller-frozen
build.

Two different directories matter once frozen, and conflating them breaks
things in different ways:

- Bundled read-only resources (templates/, static assets) live under
  `sys._MEIPASS` (onefile: a temp extraction dir; onedir: the app folder).
  Never write there — onefile wipes it on exit, onedir may be under
  Program Files with no write permission.
- Anything the app writes at runtime (the local SQLite scratch cache, the
  downloaded Chromium build) needs a stable, writable, per-user location
  that survives between runs and app updates — the OS's standard user-data
  directory, not wherever the exe happens to be sitting (Downloads,
  Desktop, a read-only volume on Mac).
"""
import sys
from pathlib import Path

_APP_NAME = "CityArtsTeacherSearch"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Directory containing bundled read-only resources (templates/, etc).
    Source checkout: repo root. Frozen: PyInstaller's extraction dir."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).parent.parent


def user_data_dir() -> Path:
    """Per-user writable directory for the local scratch DB and downloaded
    Chromium build. Source checkout: repo's data/ dir (unchanged default
    behavior). Frozen: OS-standard app-data location."""
    if not is_frozen():
        return Path(__file__).parent.parent / "data"
    if sys.platform == "win32":
        import os
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".local" / "share"
    path = base / _APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path
