# PyInstaller spec for the CityArts Teacher Finder desktop app.
# Build with: pyinstaller desktop_app.spec
# Run on windows-latest / macos-latest (see .github/workflows/build-desktop-app.yml)
# so a single spec produces the right binary for each platform's runner.
import glob
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Conda-distributed Python (e.g. Miniconda) ships several DLLs (libexpat,
# ffi, sqlite3, tcl/tk, ...) under Library/bin instead of next to python.exe,
# outside PyInstaller's default DLL search — without this, a conda-built
# exe fails at startup with "DLL load failed while importing pyexpat" etc.
# python.org / actions/setup-python builds keep these DLLs alongside the
# interpreter already, so this glob simply finds nothing there and is a
# no-op on CI.
conda_dll_dir = os.path.join(sys.base_prefix, "Library", "bin")
conda_binaries = []
if os.path.isdir(conda_dll_dir):
    for name in ("libexpat*.dll", "ffi.dll", "sqlite3.dll", "tcl86t.dll", "tk86t.dll",
                 "libbz2*.dll", "liblzma*.dll", "libcrypto*.dll", "libssl*.dll"):
        for path in glob.glob(os.path.join(conda_dll_dir, name)):
            conda_binaries.append((path, "."))

hidden_imports = (
    collect_submodules("src")
    + [
        "playwright.async_api",
    ]
)

# If left as None, PyInstaller auto-detects the target slice from
# platform.machine() of the running interpreter. That's usually right, but
# actions/setup-python installs universal2 (arm64 + x86_64 fat) Python
# builds on macOS, and PyInstaller's bootloader selection for a universal2
# interpreter has been unreliable in practice — a build on the "Intel" CI
# runner still produced an arm64-only .app, which macOS refuses to launch
# on a real Intel Mac ("not supported on this Mac"). build-desktop-app.yml
# sets PYINSTALLER_TARGET_ARCH explicitly per matrix leg ("arm64" or
# "x86_64") to remove the guesswork, and separately verifies the resulting
# binary's actual arch with `lipo` so a mismatch fails the build instead of
# shipping silently broken again.
target_arch = os.environ.get("PYINSTALLER_TARGET_ARCH") or None

# desktop_config.py (build-time-generated, holds REMOTE_INGEST_URL/SECRET —
# see build-desktop-app.yml) is imported by desktop_app.py inside a
# try/except ImportError, so a local dev build without one still runs.
# PyInstaller's modulegraph treats any try/except-ImportError-guarded import
# as intentionally optional and silently drops it from the bundle — no
# warning, no error, it just isn't there, and the frozen exe's import
# silently fails at runtime and falls back to (empty) os.environ. Confirmed
# via `pyi-archive_viewer`: a first build genuinely omitted it, and the
# packaged app silently scraped into its own local DB instead of the
# central server with no visible error (windowed exe, no console). Force
# it in explicitly whenever the file exists at build time so this can't
# regress silently again.
if os.path.exists("desktop_config.py"):
    hidden_imports.append("desktop_config")

a = Analysis(
    ["desktop_app.py"],
    pathex=["."],
    binaries=conda_binaries,
    datas=[
        ("templates", "templates"),
    ],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="CityArtsTeacherFinder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=(sys.platform == "darwin"),
    target_arch=target_arch,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="CityArtsTeacherFinder.app",
        icon=None,
        bundle_identifier="com.cityarts.teacherfinder",
        info_plist={
            "NSHighResolutionCapable": "True",
            "CFBundleShortVersionString": "1.0.0",
        },
    )
