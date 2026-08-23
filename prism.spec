# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller build spec for PRISM.

    pyinstaller prism.spec

Produces a single-machine-runnable app under dist/PRISM/ (onedir — start
with onedir, not onefile: PySide6 + the bundled seed_results data unpack
faster and are far easier to debug than a single compressed onefile binary).

Bundles prism_core/resources/data (templates, frozen dataset samples,
precomputed prompts, AND seed_results/ — the pre-computed results for all
four validated models) so a fresh install shows a populated dashboard with
no network access, no Ollama pull, and no inference run required.

.env is intentionally NOT bundled — Supabase credentials for a packaged
build should be set as real environment variables (or baked in via
--add-data/a hardcoded fallback in prism_core/config.py), never shipped as
a plaintext file inside the executable.
"""

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

datas = [
    ("prism_core/resources/data", "prism_core/resources/data"),
    ("supabase/schema.sql", "supabase"),
]

hiddenimports = collect_submodules("app") + collect_submodules("prism_core")

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "datasets", "scikit-learn", "pandas",  # research-only deps, not needed at runtime
        # PySide6 ships modules PRISM's GUI never touches - excluding them
        # shrinks the bundle and cuts PyInstaller analysis/startup time.
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
        "PySide6.QtNetwork", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
        "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
        "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSql",
        "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
        "tkinter", "unittest", "test",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PRISM",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PRISM",
)

app = BUNDLE(
    coll,
    name="PRISM.app",
    icon=None,
    bundle_identifier="com.prism.benchmark",
    info_plist={
        "NSHighResolutionCapable": "True",
        "CFBundleShortVersionString": "1.0.0",
    },
)