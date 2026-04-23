# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('icon.ico', '.')]
binaries = []
hiddenimports = ['paramiko', 'pyte', 'pyte.screens', 'pyte.streams', 'cffi', 'nacl', 'bcrypt',
                 'cryptography', 'cryptography.fernet', 'cryptography.hazmat.primitives',
                 'cryptography.hazmat.primitives.hashes', 'cryptography.hazmat.primitives.kdf.pbkdf2',
                 'sshcat', 'sshcat.theme', 'sshcat.ssh_manager', 'sshcat.threads',
                 'sshcat.terminal_widget', 'sshcat.main_window', 'sshcat.crypto',
                 'sshcat.session', 'sshcat.sftp_manager', 'sshcat.editor_widget', 'sshcat.tunnel']
tmp_ret = collect_all('paramiko')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pyte')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name='SSHcat',
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
    icon=['icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SSHcat',
)
