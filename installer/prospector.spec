# PyInstaller spec for Prospector.
#
# One-folder build rather than one-file, deliberately:
#   * it starts in about a second instead of unpacking a 60 MB archive to temp
#     on every launch
#   * antivirus flags one-file bundles far more often, and a false positive on
#     a freshly built exe is a support call nobody wants
#
# Built by build_installer.ps1, which then wraps the folder in an Inno Setup
# installer. Run it from the project root:
#     pyinstaller installer\prospector.spec --noconfirm

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent

block_cipher = None

# Flask and its dependencies resolve a lot at runtime, so their submodules are
# collected explicitly -- otherwise the frozen app dies on the first request
# with a missing-module error that never appears in testing.
hidden = []
for package in ("flask", "jinja2", "werkzeug", "click", "itsdangerous",
                "blinker", "openpyxl", "bs4", "httpx", "httpcore", "h11",
                "certifi", "anyio", "dotenv", "typer", "rich", "et_xmlfile"):
    try:
        hidden += collect_submodules(package)
    except Exception:
        hidden.append(package)

# Prospector's own modules are collected, never listed. Stages are imported
# lazily inside functions so PyInstaller's static analysis cannot see them, and
# a hand-written list silently rots every time a module is renamed.
hidden += ["prospector"] + collect_submodules("prospector")
hidden += ["encodings.idna"]

a = Analysis(
    [str(ROOT / "installer" / "prospector_launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[],
    hiddenimports=sorted(set(hidden)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Trimming the scientific stack and Tk keeps the installer under ~40 MB.
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "scipy", "PIL",
              "PySide6", "PyQt5", "notebook", "IPython", "pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Prospector",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX compression is a common antivirus trigger
    console=False,           # windowed: the UI is the browser, not a terminal
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "installer" / "prospector.ico")
        if (ROOT / "installer" / "prospector.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Prospector",
)
