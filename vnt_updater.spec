# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['vnt_updater.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='vnt_updater',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[
        '_uuid.pyd',
        '_hashlib.pyd',
        '_ssl.pyd',
        '_socket.pyd',
        'api-ms-*',
        'ext-ms-*',
        'ucrtbase.dll',
        'python3.dll',
    ],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
