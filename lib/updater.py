import os
import sys
import json
import subprocess
import tempfile
import requests
from packaging.version import Version
from lib.logger import get_logger
from version import VERSION, GITHUB_OWNER, GITHUB_REPO, APP_NAME


def get_latest_release():
    if not GITHUB_OWNER:
        return None, "GITHUB_OWNER nicht gesetzt"
    logger = get_logger()
    try:
        url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            return None, f"GitHub API: {resp.status_code}"
        data = resp.json()
        tag = data.get("tag_name", "").lstrip("v")
        return data, None
    except Exception as ex:
        logger.error(f"Update-Check fehlgeschlagen: {ex}")
        return None, str(ex)


def check_for_update(silent=False):
    logger = get_logger()
    data, error = get_latest_release()
    if error:
        if not silent:
            logger.warning(f"Update-Prüfung: {error}")
        return None, error
    tag = data.get("tag_name", "").lstrip("v")
    try:
        current = Version(VERSION)
        latest = Version(tag)
    except Exception:
        return None, "Versionen nicht vergleichbar"
    if latest > current:
        logger.info(f"Update verfügbar: v{VERSION} -> v{tag}")
        return data, None
    if not silent:
        logger.info(f"Kein Update verfügbar (v{VERSION})")
    return None, None


def download_installer(release_data, progress_callback=None):
    logger = get_logger()
    assets = release_data.get("assets", [])
    installer_name = f"{APP_NAME}-Setup.exe"
    asset = None
    for a in assets:
        if a.get("name", "").lower() == installer_name.lower():
            asset = a
            break
    if not asset:
        logger.error(f"Kein Installer '{installer_name}' im Release gefunden")
        return None
    url = asset["browser_download_url"]
    logger.info(f"Lade Installer herunter: {url}")
    try:
        resp = requests.get(url, stream=True, timeout=60)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".exe")
        temp_path = temp.name
        with open(temp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)
                if progress_callback and total > 0:
                    progress_callback(downloaded / total * 100)
        logger.info(f"Installer heruntergeladen: {temp_path}")
        return temp_path
    except Exception as ex:
        logger.error(f"Download fehlgeschlagen: {ex}")
        return None


def install_update(installer_path):
    logger = get_logger()
    if not installer_path or not os.path.exists(installer_path):
        return False
    logger.info(f"Starte Update-Installation: {installer_path}")
    try:
        subprocess.Popen(
            [installer_path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            shell=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return True
    except Exception as ex:
        logger.error(f"Installation fehlgeschlagen: {ex}")
        return False


def install_and_restart(installer_path, app_exe_path):
    logger = get_logger()
    if not installer_path or not os.path.exists(installer_path):
        return False
    logger.info(f"Starte Update-Installation mit Neustart: {installer_path} -> {app_exe_path}")
    try:
        bat_path = installer_path.replace(".exe", "_update.bat")
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write(f"""@echo off
ping -n 4 127.0.0.1 > nul
start /wait "" "{installer_path}" /SILENT /SUPPRESSMSGBOXES /NORESTART
if exist "{app_exe_path}" (
    start "" "{app_exe_path}"
)
del "%~f0" 2>nul
""")
        subprocess.Popen(
            [bat_path],
            shell=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return True
    except Exception as ex:
        logger.error(f"Update mit Neustart fehlgeschlagen: {ex}")
        return False