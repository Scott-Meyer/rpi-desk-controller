# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import (
    collect_data_files,
    copy_metadata,
    collect_submodules,
)


a = Analysis(
    ['src\\desk_controller\\desktop_agent\\main_tray.py'],
    pathex=['src'],
    binaries=[],
    datas=(
        copy_metadata('desk-controller')
        + collect_data_files('desk_controller.desktop_agent')
        + [('build\\THIRD_PARTY_LICENSES.txt', '.')]
    ),
    hiddenimports=[
        'pystray._win32',
        'tkinter',
        'tkinter.messagebox',
        'tkinter.ttk',
    ] + collect_submodules('pycaw') + collect_submodules('comtypes'),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['brainstem'],
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
    name='DeskAgent-Windows',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
