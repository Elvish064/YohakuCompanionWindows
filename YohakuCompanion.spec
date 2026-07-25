# -*- mode: python ; coding: utf-8 -*-

import os
import sys

from PyInstaller.utils.hooks import collect_submodules

hidden_imports = collect_submodules("anyio")
hidden_imports += collect_submodules("httpcore")
hidden_imports += collect_submodules("winrt.windows.media.control")
hidden_imports += collect_submodules("keyring.backends.Windows")
hidden_imports += [
    "win32pipe",
    "win32file",
    "pywintypes",
    "win32timezone",
]

openssl_binaries = []
for library_name in ("libssl-3-x64.dll", "libcrypto-3-x64.dll"):
    library_path = os.path.join(sys.prefix, "Library", "bin", library_name)
    if os.path.isfile(library_path):
        openssl_binaries.append((library_path, "."))

analysis = Analysis(
    ["launcher.py"],
    pathex=["src"],
    binaries=openssl_binaries,
    datas=[("src/yohaku_companion_windows/assets/tray.svg", "yohaku_companion_windows/assets")],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="YohakuCompanion",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=None,
    version="packaging/version_info.txt",
)
collect = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    name="YohakuCompanion",
)
