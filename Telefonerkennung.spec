# -*- mode: python ; coding: utf-8 -*-

import sys
import os
from pathlib import Path

# Python-DLL explizit finden
python_dll_dir = Path(sys.executable).parent
python_dll = python_dll_dir / "python312.dll"

# Fallback
if not python_dll.exists():
    python_dll_dir = Path(sys.prefix)
    python_dll = python_dll_dir / "python312.dll"

# Fallback glob
python_dlls = []
if python_dll.exists():
    python_dlls = [python_dll]
else:
    python_dlls = list(python_dll_dir.glob("python*.dll"))

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
    upx=False,  # WICHTIG: UPX deaktiviert, sonst wird python312.dll korrupt
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