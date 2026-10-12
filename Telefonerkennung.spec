# -*- mode: python ; coding: utf-8 -*-

import sys
import os
from pathlib import Path

# Python-DLL(s) finden - explizit im Installationsverzeichnis suchen
python_dll_dir = Path(sys.executable).parent
python_dlls = list(python_dll_dir.glob("python*.dll"))

# Fallback: Falls keine DLLs gefunden, in sys.prefix suchen
if not python_dlls:
    python_dll_dir = Path(sys.prefix)
    python_dlls = list(python_dll_dir.glob("python*.dll"))
    print(f"PyInstaller: Fallback - Python-DLL-Verzeichnis: {python_dll_dir}")

# Debug: Welche DLLs wurden gefunden?
print(f"PyInstaller: Python-DLLs gefunden: {[dll.name for dll in python_dlls]}")
print(f"PyInstaller: Python-DLL-Verzeichnis: {python_dll_dir}")

block_cipher = None

a = Analysis(
    ['telefonerkennung.py'],
    pathex=[],
    binaries=[(str(dll), '.') for dll in python_dlls],
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Telefonerkennung',
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
    icon='telefon.ico',
)