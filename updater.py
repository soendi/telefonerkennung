#!/usr/bin/env python3
"""
Updater für Telefonerkennung
Wartet auf Beendigung der alten Instanz, ersetzt EXE, startet neu.
"""
import sys
import os
import time
import subprocess
import shutil
import argparse
from pathlib import Path

def wait_for_process_exit(pid, timeout=30):
    """Wartet bis Prozess beendet ist."""
    import psutil
    try:
        proc = psutil.Process(pid)
        proc.wait(timeout=timeout)
        return True
    except (psutil.NoSuchProcess, psutil.TimeoutExpired):
        return False

def wait_for_process_exit_win32(pid, timeout=30):
    """Windows-native Prozess-Warte ohne psutil."""
    import ctypes
    from ctypes import wintypes
    
    kernel32 = ctypes.windll.kernel32
    SYNCHRONIZE = 0x00100000
    
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return True  # Prozess existiert nicht mehr
    
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 0x00000102
    
    result = kernel32.WaitForSingleObject(handle, timeout * 1000)
    kernel32.CloseHandle(handle)
    
    return result == WAIT_OBJECT_0

def replace_exe(old_exe, new_exe, max_retries=10):
    """Ersetzt alte EXE durch neue mit Retry."""
    for i in range(max_retries):
        try:
            # Backup alte EXE falls vorhanden
            backup = old_exe.with_suffix('.exe.bak')
            if old_exe.exists():
                shutil.move(str(old_exe), str(backup))
            
            # Neue EXE kopieren
            shutil.copy2(str(new_exe), str(old_exe))
            
            # Backup löschen
            if backup.exists():
                backup.unlink()
            return True
        except PermissionError:
            time.sleep(0.5)
        except Exception as e:
            print(f"Fehler beim Ersetzen: {e}")
            time.sleep(0.5)
    return False

def main():
    parser = argparse.ArgumentParser(description='Telefonerkennung Updater')
    parser.add_argument('--old-pid', type=int, required=True, help='PID der alten Instanz')
    parser.add_argument('--new-exe', required=True, help='Pfad zur neuen EXE (im Temp-Ordner)')
    parser.add_argument('--target-exe', required=True, help='Zielpfad der installierten EXE')
    parser.add_argument('--working-dir', required=True, help='Arbeitsverzeichnis (Installationsordner)')
    args = parser.parse_args()

    print(f"Updater gestartet: alte PID={args.old_pid}, new={args.new_exe}, target={args.target_exe}")

    # 1. Warten bis alte Instanz beendet
    print(f"Warte auf Beendigung der alten Instanz (PID {args.old_pid})...")
    if not wait_for_process_exit_win32(args.old_pid, timeout=30):
        print("Timeout: Alte Instanz wurde nicht beendet")
        return 1
    print("Alte Instanz beendet.")

    # 2. EXE ersetzen
    target_exe = Path(args.target_exe)
    new_exe = Path(args.new_exe)
    
    print(f"Ersetze {target_exe} durch {new_exe}...")
    if not replace_exe(target_exe, new_exe):
        print("FEHLER: EXE konnte nicht ersetzt werden")
        return 1
    print("EXE erfolgreich ersetzt.")

    # 3. Neue Instanz starten
    os.chdir(args.working_dir)
    print(f"Starte neue Instanz: {target_exe}")
    try:
        subprocess.Popen([str(target_exe)], cwd=args.working_dir, start_new_session=True)
        print("Neue Instanz gestartet.")
    except Exception as e:
        print(f"Fehler beim Starten: {e}")
        return 1
    
    return 0

if __name__ == '__main__':
    sys.exit(main())