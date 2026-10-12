# -*- mode: python ; coding: utf-8 -*-

import sys
import os
from pathlib import Path

# Spec-File für --onedir Modus (KEIN onefile!)
# Erstellt einen Ordner mit allen Dateien statt einer einzelnen EXE.
# Vermeidet ALLE Temp-Verzeichnis-Probleme und DLL-Fehler komplett.
# PyInstaller verwaltet python312.dll automatisch.

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

# EXE für das Hauptprogramm (erforderlich für COLLECT)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Telefonerkennung',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX deaktiviert - verhindert DLL-Korruption
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='telefon.ico',
)

# COLLECT für --onedir Modus: sammelt alle Dateien in einen Ordner
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Telefonerkennung',
    debug=False,
    console=False,
    icon='telefon.ico',
)