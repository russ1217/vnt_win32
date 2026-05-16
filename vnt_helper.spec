# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['vnt_helper.py'],
    pathex=[],
    binaries=[],
    datas=[('res','res')],
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
    name='vnt_helper',
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
        'vnt-cli.exe',
        'Notepad3.exe',
    ],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='vnt_helper.ico',
)
