# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for WARDOGS AUTO PTT.
# Build:  pyinstaller wardogs_voice_ptt.spec

import os
from PyInstaller.utils.hooks import collect_data_files

# sounddevice needs its bundled PortAudio DLLs (libportaudio*.dll).
# Collect the whole _sounddevice_data package to be safe.
datas = collect_data_files("_sounddevice_data")

a = Analysis(
    ["wardogs_voice_ptt.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "_sounddevice_data",
        "cffi",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="wardogs_voice_ptt",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["war-dogs.ico"],
)
