# -*- mode: python ; coding: utf-8 -*-

import sys
import os
from pathlib import Path

# Specc-File für --onedir Modus (KEIN onefile!)
# Das vermeidet ALLE Temp-Verzeichnis-Probleme und DLL-Fehler komplett.

block_cipher = None

a = Analysis(
    ['telefonerkennung.py'],
    pathex=[],
    binaries=[],  # PyInstaller verwaltet python312.dll automatisch
    datas=[
        ('telefon.ico', '.'),
    ],
    hiddenimports=[
        'pystray',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        'pywinauto',
        'comtypes',
        'requests',
        'ctypes.wintypes',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,  # PyInstaller fügt automatisch die korrekte python312.dll hinzu
    a.zipfiles,
    a.datas,
    [],
    name='Telefonerkennung',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX deaktiviert - verhindert DLL-Korruption
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='telefon.ico',
)