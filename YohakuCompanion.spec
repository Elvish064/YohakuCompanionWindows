# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules

hidden_imports = collect_submodules("winrt.windows.media.control")
hidden_imports += collect_submodules("keyring.backends.Windows")

analysis = Analysis(
    ["launcher.py"],
    pathex=["src"],
    binaries=[],
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
