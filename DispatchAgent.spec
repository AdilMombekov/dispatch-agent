# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['E:\\dispatch\\main.py'],
    pathex=[],
    binaries=[],
    datas=[('E:\\dispatch\\agent', 'agent'), ('E:\\dispatch\\ui', 'ui')],
    hiddenimports=[
        'pystray._win32', 'PIL._tkinter_finder', 'customtkinter', 'mss', 'psutil',
        # Google Drive backup — these are lazily imported in agent/backup_daemon.py
        # and agent/backup.py, so PyInstaller's static analysis misses them.
        # Without these the frozen exe logs "missing deps" and backups silently stop.
        'google.oauth2.credentials',
        'google.auth.transport.requests',
        'google_auth_oauthlib.flow',
        'googleapiclient.discovery',
        'googleapiclient.http',
        'google_auth_httplib2',
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
    name='DispatchAgent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['E:\\dispatch\\assets\\icon.ico'],
)
