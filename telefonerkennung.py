
import ctypes
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import xml.etree.ElementTree as ET
from ctypes import wintypes

from datetime import datetime, timedelta
from pathlib import Path
from tkinter import ttk, messagebox, filedialog
import csv
import logging
from logging.handlers import RotatingFileHandler

import requests
from pywinauto import Desktop
import pystray
from PIL import Image, ImageDraw


# ============================================================
# KONFIGURATION
# ============================================================

APP_VERSION = "3.2.14"

# True, sobald ein Update installiert wird: on_close beendet die App
# dann wirklich, statt sie in den Tray zu schicken.
SHUTTING_DOWN_FOR_UPDATE = False

# Globale Referenz auf die App-Instanz (für _really_quit aus Workers).
_app_instance = None

# GitHub-Repo für Update-Checks (owner/rename), z.B. "maxmuster/telefonerkennung".
# Leer lassen deaktiviert den Update-Check.
GITHUB_REPO = "soendi/telefonerkennung"

SEARCH_CH_API_KEY = "8b09c242997465ed4ca0e9176c103767"
SEARCH_CH_API_URL = "https://search.ch/tel/api/"

# API-Kontingent pro Monat (Reset jeweils am 1.).
API_QUOTA_MONTHLY = 100

POLL_INTERVAL_MS = 1000
GUI_QUEUE_INTERVAL_MS = 100
MAX_RESULTS = 200

# Daten (DB, Logs) im AppData-Verzeichnis des Benutzers.
APPDATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "Telefonerkennung"
APPDATA_DIR.mkdir(parents=True, exist_ok=True)

LOG_DIR = APPDATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

DB_FILE = APPDATA_DIR / "telefonbuch.db"

# Icon-Datei (liegt neben dem Skript bzw. in der App).
ICON_FILE = Path(__file__).resolve().with_name("telefon.ico")

APP_TITLE = "Enterprise Telephony"

UNKNOWN_NAMES = {
    "",
    "unavailable",
    "unknown",
    "unbekannt",
    "unbekannter anrufer",
    "private number",
    "anonymous",
    "anonymer anrufer",
}


# Updater-Script als String (wird bei Update im Temp-Verzeichnis geschrieben)


def _trim_log_file(path, max_lines=1000):
    """Kürzt die Logdatei beim Start auf die letzten max_lines Zeilen."""
    try:
        if not path.exists():
            return
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if len(lines) > max_lines:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines[-max_lines:])
    except OSError:
        pass


def setup_logging():
    _trim_log_file(LOG_DIR / "telefonerkennung.log", max_lines=1000)

    logger = logging.getLogger("telefonerkennung")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    file_handler = RotatingFileHandler(
        LOG_DIR / "telefonerkennung.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(console)

    return logger


logger = setup_logging()


# ============================================================
# HILFSFUNKTIONEN
# ============================================================

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def digits_only(number):
    return re.sub(r"\D", "", number or "")


def normalize_phone(number):
    """Normalisiert Schweizer Telefonnummern für Vergleiche."""
    digits = digits_only(number)

    if digits.startswith("0041"):
        digits = "0" + digits[4:]
    elif digits.startswith("41") and len(digits) == 11:
        digits = "0" + digits[2:]

    return digits


def is_external_number(number):
    return len(normalize_phone(number)) >= 9


def format_phone_display(number):
    """Formatiert eine Nummer als +41 XX XXX XX XX oder 0XX XXX XX XX."""
    raw = (number or "").strip()
    digits = digits_only(raw)

    if not digits:
        return raw

    # Auf nationale Form (0…) bringen.
    if digits.startswith("0041"):
        digits = "0" + digits[4:]
    elif digits.startswith("41") and len(digits) == 11:
        digits = "0" + digits[2:]

    # Schweizer Festnetz/Mobil: 10 Ziffern, 0AA BBB CC DD.
    if digits.startswith("0") and len(digits) == 10:
        international = (
            raw.startswith("+")
            or raw.startswith("0041")
            or (
                raw.startswith("41")
                and len(digits_only(raw)) == 11
            )
        )
        body = f"{digits[1:3]} {digits[3:6]} {digits[6:8]} {digits[8:10]}"
        return f"+41 {body}" if international else f"0{body}"

    return raw


def is_real_name(name):
    return (name or "").strip().casefold() not in UNKNOWN_NAMES


def _is_verteiler(name):
    """Prüft ob Name 'verteiler' enthält (case-insensitive)."""
    return "verteiler" in (name or "").lower()


def is_number_text(text):
    text = (text or "").strip()

    if not text or not re.fullmatch(r"[+0-9()\s./-]+", text):
        return False

    return len(digits_only(text)) >= 3


def xml_local_name(tag):
    return tag.rsplit("}", 1)[-1]


# ============================================================
# DATENBANK
# ============================================================

def connect_db():
    db = sqlite3.connect(DB_FILE, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS phonebook (
                number TEXT PRIMARY KEY,
                display_number TEXT NOT NULL,
                name TEXT NOT NULL,
                kontakt TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS unknown_numbers (
                number TEXT PRIMARY KEY,
                display_number TEXT NOT NULL,
                kontakt TEXT NOT NULL DEFAULT '',
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                reason TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS call_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT NOT NULL,
                display_number TEXT NOT NULL,
                name TEXT NOT NULL,
                kontakt TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                duration_s INTEGER NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS api_quota (
                month TEXT PRIMARY KEY,
                remaining INTEGER NOT NULL
            )
        """)


def _api_quota_month():
    return datetime.now().strftime("%Y-%m")


def get_api_quota():
    """Verbleibendes API-Kontingent des laufenden Monats (Start: 100)."""
    with connect_db() as db:
        row = db.execute(
            "SELECT remaining FROM api_quota WHERE month = ?",
            (_api_quota_month(),),
        ).fetchone()
        return row["remaining"] if row else API_QUOTA_MONTHLY


def decrement_api_quota():
    """Zieht 1 vom Monatskontingent ab (neuer Monat -> Reset auf 100).
    Gibt den Restbestand zurück."""
    month = _api_quota_month()
    with connect_db() as db:
        row = db.execute(
            "SELECT remaining FROM api_quota WHERE month = ?",
            (month,),
        ).fetchone()
        if row is None:
            db.execute("DELETE FROM api_quota")
            remaining = API_QUOTA_MONTHLY - 1
            db.execute(
                "INSERT INTO api_quota (month, remaining) VALUES (?, ?)",
                (month, remaining),
            )
        else:
            remaining = max(0, row["remaining"] - 1)
            db.execute(
                "UPDATE api_quota SET remaining = ? WHERE month = ?",
                (remaining, month),
            )
    return remaining


def migrate_add_kontakt():
    """Fügt bestehenden Tabellen die Kontakt-Spalte hinzu (Update-Pfad)."""
    for table in ("phonebook", "unknown_numbers", "call_log"):
        try:
            with connect_db() as db:
                cols = [row["name"] for row in db.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()]
                if "kontakt" not in cols:
                    db.execute(
                        f"ALTER TABLE {table} "
                        "ADD COLUMN kontakt TEXT NOT NULL DEFAULT ''"
                    )
                    logger.info(
                        "DB-Migration: Spalte kontakt in %s ergänzt.", table
                    )
        except Exception as error:
            logger.error(
                "DB-Migration für %s fehlgeschlagen: %s", table, error
            )


def get_contact(number):
    with connect_db() as db:
        return db.execute(
            "SELECT * FROM phonebook WHERE number = ?",
            (normalize_phone(number),),
        ).fetchone()


def get_unknown_entry(number):
    with connect_db() as db:
        return db.execute(
            "SELECT * FROM unknown_numbers WHERE number = ?",
            (normalize_phone(number),),
        ).fetchone()


def is_unknown(number):
    return get_unknown_entry(number) is not None


def save_contact(number, name, kontakt=None, source="Manuell"):
    normalized = normalize_phone(number)
    name = (name or "").strip()
    number = (number or "").strip()

    if not normalized:
        raise ValueError("Bitte eine gültige Telefonnummer eingeben.")

    if not name:
        raise ValueError("Der Name darf nicht leer sein.")

    # kontakt=None heisst "behalten": bestehenden Wert nicht überschreiben.
    if kontakt is None:
        try:
            existing = get_contact(number)
            kontakt = existing["kontakt"] if existing else ""
        except Exception:
            kontakt = ""
    kontakt = (kontakt or "").strip()

    with connect_db() as db:
        db.execute("""
            INSERT INTO phonebook
                (number, display_number, name, kontakt, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(number) DO UPDATE SET
                display_number = excluded.display_number,
                name = excluded.name,
                kontakt = excluded.kontakt,
                source = excluded.source,
                updated_at = excluded.updated_at
        """, (normalized, number, name, kontakt, source, now()))

        db.execute(
            "DELETE FROM unknown_numbers WHERE number = ?",
            (normalized,),
        )


def add_unknown(number, reason):
    normalized = normalize_phone(number)

    if not normalized:
        return

    timestamp = now()

    with connect_db() as db:
        db.execute("""
            INSERT INTO unknown_numbers
                (number, display_number, first_seen, last_seen, reason)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(number) DO UPDATE SET
                display_number = excluded.display_number,
                last_seen = excluded.last_seen,
                reason = excluded.reason
        """, (normalized, number, timestamp, timestamp, reason))


def delete_unknown(number):
    with connect_db() as db:
        db.execute(
            "DELETE FROM unknown_numbers WHERE number = ?",
            (normalize_phone(number),),
        )


def delete_contact(number):
    with connect_db() as db:
        db.execute(
            "DELETE FROM phonebook WHERE number = ?",
            (normalize_phone(number),),
        )


def get_all_contacts():
    with connect_db() as db:
        return db.execute("""
            SELECT number, display_number, name, kontakt, source, updated_at
            FROM phonebook
            ORDER BY name COLLATE NOCASE
        """).fetchall()


def get_all_unknown():
    with connect_db() as db:
        return db.execute("""
            SELECT number, display_number, kontakt, first_seen, last_seen, reason
            FROM unknown_numbers
            ORDER BY last_seen DESC
        """).fetchall()


def save_call_log(number, name, kontakt, started_at, duration_s):
    with connect_db() as db:
        db.execute("""
            INSERT INTO call_log
                (number, display_number, name, kontakt, started_at, duration_s)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            normalize_phone(number),
            number,
            name or "Unbekannter Anrufer",
            kontakt or "",
            started_at,
            duration_s,
        ))


def get_call_logs(limit=500):
    with connect_db() as db:
        return db.execute("""
            SELECT display_number, name, kontakt, started_at, duration_s
            FROM call_log
            ORDER BY started_at DESC
            LIMIT ?
        """, (limit,)).fetchall()


def cleanup_old_call_logs():
    """Löscht Anrufprotokolle, die älter als 30 Tage sind."""
    cutoff = (
        datetime.now() - timedelta(days=30)
    ).strftime("%Y-%m-%d %H:%M:%S")

    with connect_db() as db:
        db.execute(
            "DELETE FROM call_log WHERE started_at < ?",
            (cutoff,),
        )


# ============================================================
# SEARCH.CH
# ============================================================

def query_directory(query):
    """Liest die Suchergebnisse der search.ch-Telefonbuch-API aus."""
    response = requests.get(
        SEARCH_CH_API_URL,
        params={
            "was": query,
            "key": SEARCH_CH_API_KEY,
            "lang": "de",
            "maxnum": MAX_RESULTS,
            "privat": 1,
            "firma": 1,
        },
        timeout=10,
    )

    if response.status_code == 403:
        raise PermissionError(
            "API-Zugriff verweigert (HTTP 403). "
            "API-Schlüssel oder Berechtigung prüfen."
        )

    response.raise_for_status()
    root = ET.fromstring(response.content)

    results = []

    for entry in root.iter():
        if xml_local_name(entry.tag) != "entry":
            continue

        title = ""
        phone_numbers = []

        for element in entry.iter():
            field = xml_local_name(element.tag)
            value = (element.text or "").strip()

            if field == "title" and value and not title:
                title = value

            elif field == "phone" and value:
                phone_numbers.append(value)

        if title:
            for phone in phone_numbers:
                results.append((phone, title))

    return results


def reverse_lookup(number, allow_suffix_search=False):
    """
    Rückgabestatus:
      found      - eindeutiger exakter Treffer
      candidates - mögliche Treffer bei gekürzter Suche
      none       - kein Treffer
      ambiguous  - mehrere exakte Namen
      error      - API-Fehler
    """
    try:
        wanted = normalize_phone(number)

        # Immer zuerst die vollständige Nummer abfragen.
        results = query_directory(number)
        exact_matches = {}

        for phone, name in results:
            if (
                normalize_phone(phone) == wanted
                and is_real_name(name)
            ):
                exact_matches.setdefault(name.casefold(), name)

        if len(exact_matches) == 1:
            return (
                "found",
                next(iter(exact_matches.values())),
                "Eindeutiger Treffer für die vollständige Nummer.",
            )

        if len(exact_matches) > 1:
            return (
                "ambiguous",
                None,
                "Mehrere Namen für die vollständige Nummer gefunden.",
            )

        if not allow_suffix_search:
            return "none", None, "Kein exakter Treffer gefunden."

        # Letzte zwei Ziffern für die optionale Suche entfernen.
        prefix = wanted[:-2]

        if len(prefix) < 7:
            return (
                "none",
                None,
                "Nummer für die verkürzte Suche zu kurz.",
            )

        partial_results = query_directory(prefix)
        candidates = {}

        for phone, name in partial_results:
            candidate_number = normalize_phone(phone)

            if (
                candidate_number.startswith(prefix)
                and len(candidate_number) >= len(wanted)
                and is_real_name(name)
            ):
                candidates[(candidate_number, name.casefold())] = (
                    phone,
                    name,
                )

        if candidates:
            return (
                "candidates",
                sorted(
                    candidates.values(),
                    key=lambda item: (
                        normalize_phone(item[0]),
                        item[1].casefold(),
                    ),
                ),
                f"Teiltreffer für {prefix}; bitte manuell auswählen.",
            )

        return (
            "none",
            None,
            "Weder exakter Treffer noch passender Teiltreffer gefunden.",
        )

    except PermissionError as error:
        return "error", None, str(error)

    except requests.RequestException as error:
        return "error", None, f"Verbindungsfehler: {error}"

    except ET.ParseError:
        return "error", None, "Ungültige XML-Antwort der API."

    except Exception as error:
        return "error", None, f"Fehler bei der Rückwärtssuche: {error}"


# ============================================================
# SYSTRAY-ICON
# ============================================================

def set_window_icon(window):
    """Setzt das Telefon-Icon auf ein beliebiges Tk-Fenster."""
    try:
        if ICON_FILE.exists():
            window.iconbitmap(ICON_FILE)
    except tk.TclError:
        pass


def create_tray_image():
    try:
        if ICON_FILE.exists():
            return Image.open(ICON_FILE)
    except OSError:
        pass

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([16, 4, 48, 60], radius=8, fill="#0052a5")
    draw.rectangle([20, 12, 44, 46], fill="white")
    draw.ellipse([27, 50, 37, 58], fill="white")
    return img


# ============================================================
# Status-Werte, die auf einen aktiven Anruf hinweisen (klingelnd ODER verbunden).
# Swisscom Enterprise zeigt die Nummer während des gesamten Gesprächs an,
# der Status wechselt dabei (z. B. "Ankommender Anruf" → "Verbunden" / "Aktiv").
ACTIVE_CALL_STATUSES = {
    "Ankommender Anruf",
    "Verbunden",
    "Aktiv",
    "Im Gespräch",
    "Gespräch",
    "Connected",
    "Active",
    "In Call",
}

# Letzter gesehener Swisscom-Status (für Log bei unbekanntem Status).
_last_swisscom_status = None

class CallToast:
    def __init__(self, root):
        self.root = root
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        # -topmost allein reicht manchmal nicht über Action Center.
        # _force_topmost() wird erst in show() aufgerufen, wenn Fenster sichtbar.
        self.window.wm_attributes("-topmost", True)
        self.window.configure(bg="#1e1e1e")
        self.window.minsize(400, 0)
        self.window.maxsize(400, 99999)
        set_window_icon(self.window)

        frame = tk.Frame(self.window, bg="#1e1e1e")
        frame.pack(fill="both", expand=True)

        accent = tk.Frame(frame, bg="#0052a5", width=4)
        accent.pack(side="left", fill="y")

        content = tk.Frame(frame, bg="#1e1e1e")
        content.pack(side="left", fill="both", expand=True, padx=12, pady=10)

        self.title_label = tk.Label(
            content, text="Eingehender Anruf",
            fg="#888888", bg="#1e1e1e",
            font=("Segoe UI", 9), anchor="w",
        )
        self.title_label.pack(fill="x")

        self.name_label = tk.Label(
            content, text="",
            fg="white", bg="#1e1e1e",
            font=("Segoe UI", 14, "bold"), anchor="w",
        )
        self.name_label.pack(fill="x")

        self.name_entry = tk.Entry(
            content,
            fg="white", bg="#2a2a2a",
            insertbackground="white",
            font=("Segoe UI", 14, "bold"),
            relief="solid", bd=1,
        )
        # Entry initial versteckt (pack erst beim Bearbeiten).

        self.kontakt_label = tk.Label(
            content, text="",
            fg="white", bg="#1e1e1e",
            font=("Segoe UI", 11), anchor="w",
        )
        self.kontakt_label.pack(fill="x")

        self.kontakt_entry = tk.Entry(
            content,
            fg="white", bg="#2a2a2a",
            insertbackground="white",
            font=("Segoe UI", 11),
            relief="solid", bd=1,
        )
        # Entry initial versteckt (pack erst beim Bearbeiten).

        self.number_label = tk.Label(
            content, text="",
            fg="#aaaaaa", bg="#1e1e1e",
            font=("Segoe UI", 11), anchor="w",
        )
        self.number_label.pack(fill="x")

        self._click_callback = None
        self._edit_callback = None
        self._ring_seconds = 0
        self._ticker_after_id = None
        self._anim_after_id = None
        self._target_x = 0
        self._target_y = 0
        self._current_number = ""
        self._original_name = ""
        self._edited_name = None
        self._editing = False
        self._original_kontakt = ""
        self._edited_kontakt = None
        self._editing_kontakt = False
        self._visible = False
        self._connected = False
        self._title_prefix = "Eingehender Anruf"

        for widget in (frame, content, self.title_label,
                       self.kontakt_label, self.number_label):
            widget.bind("<Button-1>", self._on_click)

        # Doppelklick auf den Namen: inline bearbeiten.
        self.name_label.bind("<Double-Button-1>", self._start_edit)
        self.name_entry.bind("<Return>", self._commit_edit)
        self.name_entry.bind("<Escape>", self._cancel_edit)
        self.name_entry.bind("<FocusOut>", self._commit_edit)

        # Doppelklick auf den Kontakt: inline bearbeiten.
        self.kontakt_label.bind("<Double-Button-1>", self._start_kontakt_edit)
        self.kontakt_entry.bind("<Return>", self._commit_kontakt_edit)
        self.kontakt_entry.bind("<Escape>", self._cancel_kontakt_edit)
        self.kontakt_entry.bind("<FocusOut>", self._commit_kontakt_edit)

    def _force_topmost(self):
        """Hält den Toast über anderen Fenstern (auch Action Center).
        Absichtlich nur Tk-eigene Mittel: winfo_id() kann auf Windows
        ein veraltetes Handle liefern, Tk selbst kennt das richtige.
        Nimmt keinen Fokus weg."""
        try:
            self.window.wm_attributes("-topmost", True)
            self.window.lift()
        except Exception:
            pass

    def set_click_callback(self, callback):
        self._click_callback = callback

    def set_edit_callback(self, callback):
        """callback(number, name) – wird beim Ausblenden mit
        geändertem Namen aufgerufen."""
        self._edit_callback = callback

    def _on_click(self, _event=None):
        if self._editing or self._editing_kontakt:
            return
        if self._click_callback:
            self._click_callback()

    # ---------------------------------------------------------
    # NAME INLINE BEARBEITEN (DOPPELKLICK)
    # ---------------------------------------------------------

    def _start_edit(self, _event=None):
        if self._editing:
            return "break"

        self._editing = True
        self.name_label.pack_forget()
        self.name_entry.pack(fill="x")
        self.name_entry.delete(0, "end")
        self.name_entry.insert(
            0, self.name_label.cget("text") or ""
        )
        self.name_entry.select_range(0, "end")
        self.name_entry.focus_set()
        return "break"

    def _commit_edit(self, _event=None):
        if not self._editing:
            return

        self._editing = False
        new_name = self.name_entry.get().strip()

        self.name_entry.pack_forget()
        self.name_label.pack(fill="x", before=self.kontakt_label)

        if new_name and new_name != self._original_name:
            self._edited_name = new_name
            self.name_label.config(text=new_name)
        elif new_name:
            self.name_label.config(text=new_name)

    def _cancel_edit(self, _event=None):
        self._editing = False
        self.name_entry.pack_forget()
        self.name_label.pack(fill="x", before=self.kontakt_label)
        return "break"

    # ---------------------------------------------------------
    # KONTAKT INLINE BEARBEITEN (DOPPELKLICK)
    # ---------------------------------------------------------

    def _start_kontakt_edit(self, _event=None):
        if self._editing_kontakt:
            return "break"

        self._editing_kontakt = True
        self.kontakt_label.pack_forget()
        self.kontakt_entry.pack(fill="x", before=self.number_label)
        self.kontakt_entry.delete(0, "end")
        self.kontakt_entry.insert(
            0, self.kontakt_label.cget("text") or ""
        )
        self.kontakt_entry.select_range(0, "end")
        self.kontakt_entry.focus_set()
        return "break"

    def _commit_kontakt_edit(self, _event=None):
        if not self._editing_kontakt:
            return

        self._editing_kontakt = False
        new_kontakt = self.kontakt_entry.get().strip()

        self.kontakt_entry.pack_forget()
        self.kontakt_label.pack(fill="x", before=self.number_label)

        if new_kontakt != self._original_kontakt:
            self._edited_kontakt = new_kontakt
            self.kontakt_label.config(text=new_kontakt)
        else:
            self.kontakt_label.config(text=new_kontakt)

    def _cancel_kontakt_edit(self, _event=None):
        self._editing_kontakt = False
        self.kontakt_entry.pack_forget()
        self.kontakt_label.pack(fill="x", before=self.number_label)
        return "break"

    def show(self, name, number, status=None, kontakt=""):
        logger.info("TOAST SHOW aufgerufen: name=%s, number=%s, status=%r", name, number, status)
        # Restlichen Editierzustand zurücksetzen.
        if self._editing:
            self._cancel_edit()
        if self._editing_kontakt:
            self._cancel_kontakt_edit()
        self._edited_name = None
        self._edited_kontakt = None
        self._original_name = name or "Unbekannter Anrufer"
        self._original_kontakt = kontakt or ""
        self._current_number = number or ""
        self._visible = True
        self._connected = status is not None and status != "Ankommender Anruf"
        self._title_prefix = "Verbunden" if self._connected else "Eingehender Anruf"

        self.name_label.config(text=self._original_name)
        self.kontakt_label.config(text=self._original_kontakt)
        self.number_label.config(text=format_phone_display(self._current_number))
        self._ring_seconds = 0
        self.title_label.config(text=f"{self._title_prefix} (0 s)")
        self._calc_target()
        # Zielposition sofort setzen: Auch wenn die Animation
        # fehlschlägt, steht der Toast danach an der richtigen Stelle.
        try:
            self.window.geometry(f"+{self._target_x}+{self._target_y}")
        except Exception:
            pass
        self.window.deiconify()
        self.window.lift()
        self._force_topmost()
        self._start_ticker()
        try:
            self._animate_in()
        except Exception as e:
            logger.error("TOAST Animationsfehler in show(): %s", e)
        # Sicherheitsnetz: Falls die Animation hängen bleibt,
        # Toast an der Zielposition sicherstellen.
        try:
            self.window.after(
                self.ANIM_STEPS * self.ANIM_DELAY_MS + 300,
                self._ensure_visible,
            )
        except Exception:
            pass
        logger.info("TOAST: show() abgeschlossen, Fenster sollte sichtbar sein")

    def _ensure_visible(self):
        """Stellt sicher, dass ein aktiver Toast sichtbar an der
        Zielposition steht (Fallback falls Animation hängt)."""
        try:
            if not getattr(self, "_visible", False):
                return
            if not self._current_number:
                return
            self.window.geometry(f"+{self._target_x}+{self._target_y}")
            self.window.deiconify()
            self.window.lift()
            self._force_topmost()
        except Exception:
            pass

    def update_status(self, status):
        """Aktualisiert Klingeln/Verbunden während eines laufenden Anrufs."""
        try:
            connected = status is not None and status != "Ankommender Anruf"
            prefix = "Verbunden" if connected else "Eingehender Anruf"
            if getattr(self, "_title_prefix", None) == prefix and getattr(self, "_connected", None) == connected:
                return
            self._connected = connected
            self._title_prefix = prefix
            self.title_label.config(text=f"{prefix} ({self._ring_seconds} s)")
            logger.info("TOAST Statuswechsel: %r", status)
        except Exception:
            pass

    def _start_ticker(self):
        self._stop_ticker()
        self._tick()

    def _tick(self):
        self._ring_seconds += 1
        prefix = getattr(self, "_title_prefix", "Eingehender Anruf")
        self.title_label.config(
            text=f"{prefix} ({self._ring_seconds} s)"
        )
        # Toast über Action Center halten: Topmost regelmässig erneuern.
        try:
            self.window.lift()
            self._force_topmost()
        except Exception:
            pass
        self._ticker_after_id = self.window.after(1000, self._tick)

    def _stop_ticker(self):
        if self._ticker_after_id is not None:
            try:
                self.window.after_cancel(self._ticker_after_id)
            except Exception:
                pass
            self._ticker_after_id = None

    def update_name(self, name):
        # Manuell bearbeiteten Namen nicht überschreiben.
        if self._editing or self._edited_name is not None:
            return
        self.name_label.config(text=name or "Unbekannter Anrufer")

    def hide(self):
        self._visible = False
        self._stop_ticker()

        # Editierten Namen/Kontakt speichern, bevor das Fenster schliesst.
        if self._editing:
            self._commit_edit()
        if self._editing_kontakt:
            self._commit_kontakt_edit()

        if (
            (self._edited_name or self._edited_kontakt is not None)
            and self._current_number
            and self._edit_callback
        ):
            try:
                self._edit_callback(
                    self._current_number,
                    self._edited_name,
                    self._edited_kontakt,
                )
            except Exception:
                pass
            self._edited_name = None
            self._edited_kontakt = None

        self._animate_out()

    # ---------------------------------------------------------
    # SLIDE-ANIMATION
    # ---------------------------------------------------------

    ANIM_STEPS = 15
    ANIM_DELAY_MS = 15

    def _calc_target(self):
        """Berechnet die Zielposition: horizontal zentriert, oben am Bildschirm."""
        self.window.update_idletasks()
        w = 400
        h = self.window.winfo_reqheight()
        if not h or h < 50:
            h = 160
        if h > 600:
            h = 600
        # Breite fest erzwingen (overrideredirect ignoriert minsize).
        self.window.geometry(f"{w}x{h}")
        self.window.update_idletasks()
        screen_w = self.root.winfo_screenwidth()

        # Arbeitsbereich (ohne Taskleiste) über Win32-API holen.
        try:
            import ctypes
            from ctypes import wintypes

            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", wintypes.LONG),
                    ("top", wintypes.LONG),
                    ("right", wintypes.LONG),
                    ("bottom", wintypes.LONG),
                ]

            rect = RECT()
            if ctypes.windll.user32.SystemParametersInfoW(
                0x0030, 0, ctypes.byref(rect), 0
            ):
                if rect.right - rect.left > w:
                    screen_w = rect.right - rect.left
        except Exception:
            pass

        if not screen_w or screen_w < w + 50:
            screen_w = w + 50
        x = (screen_w - w) // 2
        if x < 0:
            x = 0
        y = 0

        self._target_x = x
        self._target_y = y
        self._target_h = h
        self._screen_w = screen_w

    def _stop_anim(self):
        if self._anim_after_id is not None:
            try:
                self.window.after_cancel(self._anim_after_id)
            except Exception:
                pass
            self._anim_after_id = None

    def _animate_in(self):
        """Fliegt von oben (Bildschirmrand) nach unten an den Zielort."""
        self._stop_anim()
        x = self._target_x
        start_y = -self._target_h
        end_y = self._target_y
        steps = self.ANIM_STEPS

        def step(i):
            try:
                # Ease-out: grosse Schritte am Anfang, kleine am Ende.
                progress = 1 - (1 - i / steps) ** 2
                y = int(start_y + (end_y - start_y) * progress)
                self.window.geometry(f"+{x}+{y}")
                if i < steps:
                    self._anim_after_id = self.window.after(
                        self.ANIM_DELAY_MS, step, i + 1
                    )
                else:
                    self._anim_after_id = None
                    # Sicherstellen, dass Fenster am Ende sichtbar
                    # an der Zielposition steht.
                    self.window.geometry(f"+{x}+{end_y}")
                    self.window.deiconify()
                    self.window.lift()
                    self._force_topmost()
            except Exception as e:
                logger.error("TOAST Animationsfehler: %s", e)
                # Fallback: Fenster direkt an Zielposition
                try:
                    self.window.geometry(f"+{x}+{end_y}")
                    self.window.deiconify()
                    self.window.lift()
                    self._force_topmost()
                except Exception:
                    pass

        step(0)

    def _animate_out(self):
        """Fliegt von der Zielposition nach oben zum Bildschirmrand hinaus."""
        self._stop_anim()
        self._calc_target()
        x = self._target_x
        start_y = self._target_y
        end_y = -self._target_h
        steps = self.ANIM_STEPS

        def step(i):
            try:
                # Ease-in: kleine Schritte am Anfang, grosse am Ende.
                progress = (i / steps) ** 2
                y = int(start_y + (end_y - start_y) * progress)
                self.window.geometry(f"+{x}+{y}")
                if i < steps:
                    self._anim_after_id = self.window.after(
                        self.ANIM_DELAY_MS, step, i + 1
                    )
                else:
                    self._anim_after_id = None
                    self.window.withdraw()
            except Exception as e:
                logger.error("TOAST Animationsfehler: %s", e)
                try:
                    self.window.withdraw()
                except Exception:
                    pass
                self._anim_after_id = None

        step(0)

    def _position(self):
        self._calc_target()
        self.window.geometry(f"+{self._target_x}+{self._target_y}")

    def destroy(self):
        self._stop_ticker()
        self._stop_anim()
        self.window.destroy()


# ============================================================
# SWISSCOM-OBERFLÄCHE AUSLESEN
# ============================================================

def get_incoming_call():
    """
    Liest den aktuellen eingehenden Anruf aus.
    Diese Funktion läuft ausschliesslich im Hintergrundthread.
    """
    try:
        window = Desktop(backend="uia").window(
            title_re=f".*{APP_TITLE}.*"
        )

        if not window.exists(timeout=0.1):
            return None

        status = None
        panel = None

        for element in window.descendants():
            try:
                automation_id = element.element_info.automation_id

                if automation_id == "ConvoStatusTB":
                    status = element.window_text()

                elif automation_id == "MyPanel":
                    panel = element

            except Exception:
                continue

        global _last_swisscom_status
        if _last_swisscom_status != status:
            _last_swisscom_status = status
            if status is not None and panel is not None and status not in ACTIVE_CALL_STATUSES:
                logger.info("*** Swisscom-Status (kein aktiver Anruf): %r ***", status)

        if status not in ACTIVE_CALL_STATUSES or panel is None:
            return None

        texts = []

        for element in panel.descendants():
            try:
                if element.element_info.control_type == "Text":
                    value = element.window_text().strip()

                    if (
                        value
                        and value != status
                        and value not in texts
                    ):
                        texts.append(value)

            except Exception:
                continue

        number = next(
            (value for value in texts if is_number_text(value)),
            None,
        )

        if not number:
            return None

        name = next(
            (
                value for value in texts
                if value != number
                and not is_number_text(value)
                and is_real_name(value)
            ),
            "Unbekannter Anrufer",
        )

        return {"name": name, "nummer": number, "status": status}

    except Exception as error:
        logger.error("Fehler beim Lesen der Swisscom-App: %s", error)
        return None


# ============================================================
# HAUPTPROGRAMM
# ============================================================

class CallerIDApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"Swisscom Telefonerkennung v{APP_VERSION}")
        self.root.geometry("920x700")
        self.root.minsize(780, 560)
        set_window_icon(self.root)

        # Sofort ins Tray: kein Taskbar-Icon, bis das Fenster
        # explizit aus dem Tray geöffnet wird.
        self._in_tray = True
        self.root.withdraw()

        self.last_call_id = None
        self.pending = set()
        self.current_unknown_number = None

        # Anrufprotokoll: Startzeit und Anruferdaten des laufenden Anrufs.
        self.call_started_at = None
        self.call_log_name = ""
        self.call_log_kontakt = ""
        self.call_log_number = ""
        # Zähler für aufeinanderfolgende Leer-Polls: Erst nach 2x None
        # gilt ein Anruf als wirklich beendet (Schutz vor Lese-Aussetzern).
        self._none_count = 0

        # Testmodus für simulierte Anrufe.
        self.test_mode = False
        self.test_call_data = None

        # Hintergrundthread und GUI kommunizieren über diese Queue.
        self.ui_queue = queue.Queue()
        self.stop_event = threading.Event()

        self.durchwahl_suche = tk.BooleanVar(value=False)

        # Toast und Systray einrichten.
        self.toast = CallToast(root)
        self.toast.set_click_callback(
            lambda: self.ui_queue.put(("tray", "open"))
        )
        self.toast.set_edit_callback(self.on_toast_name_edit)

        self.tray_icon = None
        self.setup_tray()

        # Minimieren (Taskleiste/Schliessen-X) → ins Tray statt Taskbar.
        self.root.bind("<Unmap>", self._on_unmap)
        self.root.iconify = self._iconify_to_tray

        self.build_ui()
        self.refresh_lists()
        self.refresh_api_quota()

        # GUI-Queue regelmässig im GUI-Thread abarbeiten.
        self.root.after(GUI_QUEUE_INTERVAL_MS, self.process_ui_queue)

        # Swisscom-Überwachung in eigenem Thread starten.
        self.poll_thread = threading.Thread(
            target=self.poll_worker,
            daemon=True,
        )
        self.poll_thread.start()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # --------------------------------------------------------
    # SYSTRAY
    # --------------------------------------------------------

    def setup_tray(self):
        try:
            image = create_tray_image()
            menu = pystray.Menu(
                pystray.MenuItem(
                    "Einstellungen öffnen",
                    self.on_tray_open,
                    default=True,
                ),
                pystray.MenuItem(
                    "Testanruf simulieren",
                    self.on_tray_test,
                ),
                pystray.MenuItem(
                    "Auf Updates prüfen",
                    self.on_tray_update_check,
                ),
                pystray.MenuItem("Beenden", self.on_tray_exit),
            )
            self.tray_icon = pystray.Icon(
                "telefonerkennung",
                image,
                "Swisscom Telefonerkennung",
                menu,
            )
            threading.Thread(
                target=self.tray_icon.run,
                daemon=True,
            ).start()
        except Exception as error:
            print(f"Fehler beim Systray-Icon: {error}")
            self.root.deiconify()

    def on_tray_open(self):
        self.ui_queue.put(("tray", "open"))

    def on_tray_exit(self):
        self.ui_queue.put(("tray", "exit"))

    def on_tray_test(self):
        logger.info("TEST: on_tray_test aufgerufen")
        self.ui_queue.put(("tray", "test"))

    def on_tray_update_check(self):
        self.ui_queue.put(("tray", "update_check"))

    def start_update_check(self):
        """Prüft im Hintergrund auf Updates und installiert ggf."""
        def _worker():
            result = check_for_update()
            if not result:
                self.root.after(
                    0,
                    lambda: messagebox.showinfo(
                        "Update-Check",
                        f"Kein Update gefunden.\n"
                        f"Aktuelle Version: {APP_VERSION}",
                    ),
                )
                return

            version, url = result
            logger.info("Update gefunden: %s", version)

            def _ask_and_install():
                if messagebox.askyesno(
                    "Update verfügbar",
                    f"Eine neue Version ({version}) ist verfügbar.\n"
                    f"Aktuelle Version: {APP_VERSION}\n\n"
                    "Jetzt herunterladen und installieren?\n"
                    "Die App wird danach beendet.",
                ):
                    def _install_worker():
                        download_and_install_update(url, version)

                    threading.Thread(
                        target=_install_worker, daemon=True
                    ).start()

            self.root.after(0, _ask_and_install)

        threading.Thread(target=_worker, daemon=True).start()

    def simulate_call(self):
        logger.info(f"TEST: simulate_call aufgerufen, test_mode={self.test_mode}")
        if self.test_mode:
            logger.info("TEST: test_mode bereits aktiv, breche ab")
            return

        self.test_mode = True
        self.test_call_data = {
            "name": "Unbekannter Anrufer",
            "nummer": "+41817564572",
        }
        logger.info(f"TEST: test_mode gesetzt, Daten={self.test_call_data}")

        # Test-Daten sofort in Queue legen, damit Toast sofort angezeigt wird
        self.ui_queue.put(("poll", self.test_call_data))

        # Testanruf nach 15 Sekunden automatisch beenden.
        self.root.after(15000, self.end_test_call)
        logger.info("TEST: after(15000, end_test_call) gesetzt")

    def end_test_call(self):
        logger.info("TEST: end_test_call – Testanruf wird beendet (test_mode=False)")
        self.test_mode = False
        self.test_call_data = None

        # Toast direkt ausblenden (läuft im GUI-Thread via root_after).
        if self.last_call_id is not None:
            logger.info("TRACE: blende Toast direkt nach Testende aus")
            self.toast.hide()
            self._log_call_end()
            self.set_call_display("Warte auf eingehenden Anruf ...")
            self.last_call_id = None
            logger.info("TRACE: toast.hide() ausgeführt")

    def show_settings(self):
        self._show_from_tray()

    def _show_from_tray(self):
        """Fenster aus dem Tray holen – Taskbar-Icon erscheint."""
        self._in_tray = False
        self.root.deiconify()
        self.root.state("normal")
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.focus_force()
        self.root.after(200, lambda: self.root.attributes("-topmost", False))

    def _hide_to_tray(self):
        """Fenster ins Tray versenken – Taskbar-Icon verschwindet."""
        self._in_tray = True
        self.root.withdraw()

    def _iconify_to_tray(self):
        """Minimieren (Iconify) → ins Tray statt in die Taskleiste."""
        if self._in_tray:
            return
        self._hide_to_tray()

    def _on_unmap(self, event):
        """Fängt Minimieren ab und versenkt das Fenster ins Tray."""
        if event.widget != self.root or self._in_tray:
            return
        try:
            if self.root.state() == "iconic":
                self.root.deiconify()
                self.root.state("normal")
                self._hide_to_tray()
        except tk.TclError:
            pass

    def exit_app(self):
        self.stop_event.set()
        self.toast.hide()
        self.toast.destroy()
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        self.root.destroy()

    # --------------------------------------------------------
    # GUI AUFBAUEN
    # --------------------------------------------------------

    def build_ui(self):
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=10)
        # Beim Tab-Wechsel: Anrufprotokoll-Sortierung auf "Zeit" zurücksetzen
        self.tabs.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # TAB: Aktueller Anruf
        self.call_tab = ttk.Frame(self.tabs, padding=20)
        self.tabs.add(self.call_tab, text="Aktueller Anruf")

        ttk.Label(
            self.call_tab,
            text="Swisscom Telefonerkennung",
            font=("Segoe UI", 18, "bold"),
        ).pack(pady=(10, 18))

        self.call_name = ttk.Label(
            self.call_tab,
            text="Warte auf eingehenden Anruf ...",
            font=("Segoe UI", 16),
            wraplength=760,
        )
        self.call_name.pack(pady=8)

        self.call_number = ttk.Label(
            self.call_tab,
            text="",
            font=("Segoe UI", 12),
        )
        self.call_number.pack(pady=5)

        self.call_result = ttk.Label(
            self.call_tab,
            text="",
            wraplength=760,
            justify="center",
        )
        self.call_result.pack(pady=12)

        ttk.Checkbutton(
            self.call_tab,
            text=(
                "Durchwahl-Suche aktivieren "
                "(letzte 2 Ziffern weglassen)"
            ),
            variable=self.durchwahl_suche,
        ).pack(pady=8)

        ttk.Label(
            self.call_tab,
            text=f"Lokale Datenbank: {DB_FILE}",
            wraplength=760,
        ).pack(side="bottom", pady=8)

        self.api_quota_var = tk.StringVar()
        ttk.Label(
            self.call_tab,
            textvariable=self.api_quota_var,
            font=("Segoe UI", 10, "bold"),
            wraplength=760,
        ).pack(side="bottom", pady=(8, 0))

        # TAB: Telefonbuch
        self.book_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.book_tab, text="Telefonbuch")

        ttk.Label(
            self.book_tab,
            text="Lokal gespeicherte Kontakte",
            font=("Segoe UI", 12, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        book_frame = ttk.Frame(self.book_tab)
        book_frame.pack(fill="both", expand=True)

        self.book_tree = ttk.Treeview(
            book_frame,
            columns=("number", "name", "kontakt", "source"),
            show="headings",
            selectmode="browse",
        )

        self.book_tree.heading("number", text="Telefonnummer")
        self.book_tree.heading("name", text="Name")
        self.book_tree.heading("kontakt", text="Kontakt")
        self.book_tree.heading("source", text="Quelle")

        self.book_tree.column("number", width=160)
        self.book_tree.column("name", width=220)
        self.book_tree.column("kontakt", width=180)
        self.book_tree.column("source", width=150)

        book_scroll = ttk.Scrollbar(
            book_frame,
            orient="vertical",
            command=self.book_tree.yview,
        )
        self.book_tree.configure(yscrollcommand=book_scroll.set)

        self.book_tree.pack(side="left", fill="both", expand=True)
        book_scroll.pack(side="right", fill="y")

        self.book_tree.bind(
            "<<TreeviewSelect>>",
            self.on_contact_selected,
        )

        # Bearbeitungsfelder
        edit_contact_frame = ttk.LabelFrame(
            self.book_tab,
            text="Kontakt bearbeiten",
            padding=10,
        )
        edit_contact_frame.pack(fill="x", pady=(10, 0))

        self.edit_original_number = None
        self.edit_number_var = tk.StringVar()
        self.edit_name_var = tk.StringVar()
        self.edit_kontakt_var = tk.StringVar()

        ttk.Label(
            edit_contact_frame,
            text="Telefonnummer:",
        ).grid(row=0, column=0, sticky="w", padx=5, pady=4)

        ttk.Entry(
            edit_contact_frame,
            textvariable=self.edit_number_var,
        ).grid(row=0, column=1, sticky="ew", padx=5, pady=4)

        ttk.Label(
            edit_contact_frame,
            text="Name:",
        ).grid(row=1, column=0, sticky="w", padx=5, pady=4)

        ttk.Entry(
            edit_contact_frame,
            textvariable=self.edit_name_var,
        ).grid(row=1, column=1, sticky="ew", padx=5, pady=4)

        ttk.Label(
            edit_contact_frame,
            text="Kontakt:",
        ).grid(row=2, column=0, sticky="w", padx=5, pady=4)

        ttk.Entry(
            edit_contact_frame,
            textvariable=self.edit_kontakt_var,
        ).grid(row=2, column=1, sticky="ew", padx=5, pady=4)

        edit_contact_frame.columnconfigure(1, weight=1)

        contact_buttons = ttk.Frame(edit_contact_frame)
        contact_buttons.grid(
            row=3, column=0, columnspan=2,
            sticky="e", padx=5, pady=8,
        )

        ttk.Button(
            contact_buttons,
            text="Änderungen speichern",
            command=self.update_selected_contact,
        ).pack(side="left", padx=4)

        ttk.Button(
            contact_buttons,
            text="Ausgewählten Kontakt löschen",
            command=self.delete_selected_contact,
        ).pack(side="left", padx=4)

        import_export_buttons = ttk.Frame(self.book_tab)
        import_export_buttons.pack(fill="x", pady=(10, 0))

        ttk.Button(
            import_export_buttons,
            text="Telefonbuch exportieren (CSV)",
            command=self.export_phonebook,
        ).pack(side="left", padx=4)

        ttk.Button(
            import_export_buttons,
            text="Telefonbuch importieren (CSV)",
            command=self.import_phonebook,
        ).pack(side="left", padx=4)

        # TAB: Unbekannte Nummern
        self.unknown_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.unknown_tab, text="Unbekannte Nummern")

        ttk.Label(
            self.unknown_tab,
            text="Unbekannte Nummern bearbeiten",
            font=("Segoe UI", 12, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        unknown_frame = ttk.Frame(self.unknown_tab)
        unknown_frame.pack(fill="both", expand=True)

        self.unknown_tree = ttk.Treeview(
            unknown_frame,
            columns=("number", "kontakt", "seen", "reason"),
            show="headings",
            selectmode="browse",
            height=9,
        )

        self.unknown_tree.heading("number", text="Telefonnummer")
        self.unknown_tree.heading("kontakt", text="Kontakt")
        self.unknown_tree.heading("seen", text="Zuletzt gesehen")
        self.unknown_tree.heading("reason", text="Suchergebnis")

        self.unknown_tree.column("number", width=150)
        self.unknown_tree.column("kontakt", width=180)
        self.unknown_tree.column("seen", width=140)
        self.unknown_tree.column("reason", width=280)

        unknown_scroll = ttk.Scrollbar(
            unknown_frame,
            orient="vertical",
            command=self.unknown_tree.yview,
        )
        self.unknown_tree.configure(yscrollcommand=unknown_scroll.set)

        self.unknown_tree.pack(side="left", fill="both", expand=True)
        unknown_scroll.pack(side="right", fill="y")

        self.unknown_tree.bind(
            "<<TreeviewSelect>>",
            self.on_unknown_selected,
        )

        edit_frame = ttk.LabelFrame(
            self.unknown_tab,
            text="Ausgewählte Nummer",
            padding=10,
        )
        edit_frame.pack(fill="x", pady=(10, 0))

        ttk.Label(
            edit_frame,
            text="Nummer:",
        ).grid(row=0, column=0, sticky="w", padx=5, pady=4)

        self.unknown_number_var = tk.StringVar()

        ttk.Entry(
            edit_frame,
            textvariable=self.unknown_number_var,
            state="readonly",
        ).grid(row=0, column=1, sticky="ew", padx=5, pady=4)

        ttk.Label(
            edit_frame,
            text="Name:",
        ).grid(row=1, column=0, sticky="w", padx=5, pady=4)

        self.unknown_name_var = tk.StringVar()

        ttk.Entry(
            edit_frame,
            textvariable=self.unknown_name_var,
        ).grid(row=1, column=1, sticky="ew", padx=5, pady=4)

        ttk.Label(
            edit_frame,
            text="Kontakt:",
        ).grid(row=2, column=0, sticky="w", padx=5, pady=4)

        self.unknown_kontakt_var = tk.StringVar()

        ttk.Entry(
            edit_frame,
            textvariable=self.unknown_kontakt_var,
        ).grid(row=2, column=1, sticky="ew", padx=5, pady=4)

        edit_frame.columnconfigure(1, weight=1)

        unknown_buttons = ttk.Frame(edit_frame)
        unknown_buttons.grid(
            row=3, column=0, columnspan=2,
            sticky="e", padx=5, pady=8,
        )

        ttk.Button(
            unknown_buttons,
            text="Name speichern → Telefonbuch",
            command=self.save_unknown_as_contact,
        ).pack(side="left", padx=4)

        ttk.Button(
            unknown_buttons,
            text="Erneut online suchen",
            command=self.retry_selected_lookup,
        ).pack(side="left", padx=4)

        ttk.Button(
            unknown_buttons,
            text="Nummer löschen",
            command=self.delete_selected_unknown,
        ).pack(side="left", padx=4)

        # TAB: Anrufprotokoll
        self.log_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.log_tab, text="Anrufprotokoll")

        ttk.Label(
            self.log_tab,
            text="Alle protokollierten Telefonanrufe",
            font=("Segoe UI", 12, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        log_frame = ttk.Frame(self.log_tab)
        log_frame.pack(fill="both", expand=True)

        # Spalten: Datum (dd.mm.jjjj), Zeit (hh:mm), Nummer, Name, Kontakt, Dauer (mm:ss)
        self.log_tree = ttk.Treeview(
            log_frame,
            columns=("date", "time", "number", "name", "kontakt", "duration"),
            show="headings",
            selectmode="browse",
        )

        self.log_tree.heading("date", text="Datum", command=lambda: self._sort_log_tree("date"))
        self.log_tree.heading("time", text="Zeit", command=lambda: self._sort_log_tree("time"))
        self.log_tree.heading("number", text="Telefonnummer", command=lambda: self._sort_log_tree("number"))
        self.log_tree.heading("name", text="Name", command=lambda: self._sort_log_tree("name"))
        self.log_tree.heading("kontakt", text="Kontakt", command=lambda: self._sort_log_tree("kontakt"))
        self.log_tree.heading("duration", text="Dauer", command=lambda: self._sort_log_tree("duration"))

        self.log_tree.column("date", width=110, anchor="center")
        self.log_tree.column("time", width=60, anchor="center")
        self.log_tree.column("number", width=140)
        self.log_tree.column("name", width=180)
        self.log_tree.column("kontakt", width=150)
        self.log_tree.column("duration", width=80, anchor="e")

        log_scroll = ttk.Scrollbar(
            log_frame,
            orient="vertical",
            command=self.log_tree.yview,
        )
        self.log_tree.configure(yscrollcommand=log_scroll.set)

        # Sortier-Status merken: Standard nach Datum+Zeit absteigend
        self._log_sort_column = "date"
        self._log_sort_desc = True

        self.log_tree.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

    # --------------------------------------------------------
    # GUI-QUEUE: GUI NUR IM HAUPTTHREAD AKTUALISIEREN
    # --------------------------------------------------------

    def process_ui_queue(self):
        if self.stop_event.is_set():
            return

        latest_poll = None
        poll_seen = False

        try:
            while True:
                event = self.ui_queue.get_nowait()

                if event[0] == "poll":
                    latest_poll = event[1]
                    poll_seen = True

                elif event[0] == "lookup_done":
                    self.finish_lookup(*event[1:])

                elif event[0] == "tray":
                    if event[1] == "open":
                        self.show_settings()
                    elif event[1] == "test":
                        self.simulate_call()
                    elif event[1] == "update_check":
                        self.start_update_check()
                    elif event[1] == "exit":
                        self.exit_app()
                        return

                elif event[0] == "quit":
                    # Aus einem Worker-Thread angefordert – hier
                    # laufen wir sicher im Hauptthread.
                    self._really_quit()
                    return

        except queue.Empty:
            pass
        except Exception:
            import traceback
            print("TRACE: EXCEPTION in process_ui_queue:")
            traceback.print_exc()

        # Poll-Ergebnis immer verarbeiten – auch None (Anruf vorbei),
        # damit process_call das Ausblenden ausloesen kann.
        if poll_seen:
            try:
                self.process_call(latest_poll)
            except Exception:
                import traceback
                print("TRACE: EXCEPTION in process_call:")
                traceback.print_exc()

        self.root.after(
            GUI_QUEUE_INTERVAL_MS,
            self.process_ui_queue,
        )

    def _on_tab_changed(self, event):
        """Bei Tab-Wechsel: Anrufprotokoll-Sortierung auf 'Datum' zurücksetzen."""
        if self.tabs.select() == str(self.log_tab):
            self._log_sort_column = "date"
            self._log_sort_desc = True
            self.refresh_call_log()

    def poll_worker(self):
        """Liest die Swisscom-Oberfläche ausserhalb des GUI-Threads."""
        while not self.stop_event.is_set():
            if self.test_mode:
                call = self.test_call_data
                # Nur senden, wenn Test-Daten vorhanden sind
                if call is not None:
                    self.ui_queue.put(("poll", call))
            else:
                call = get_incoming_call()
                self.ui_queue.put(("poll", call))
            self.stop_event.wait(POLL_INTERVAL_MS / 1000)

    def on_close(self):
        # Während eines Updates: App wirklich beenden, damit der
        # Installer die Dateien ersetzen kann (CloseApplications).
        if SHUTTING_DOWN_FOR_UPDATE:
            self._really_quit()
            return
        # Sonst: Fenster nur verstecken – Programm laeuft im Systray weiter.
        self._hide_to_tray()

    def _really_quit(self):
        """App vollständig beenden (kein Tray).

        Läuft bereits im Hauptthread (on_close) oder wird per
        ui_queue-Event angefordert – niemals direkt aus einem
        Worker-Thread heraus.
        """
        global SHUTTING_DOWN_FOR_UPDATE
        SHUTTING_DOWN_FOR_UPDATE = True
        try:
            if self.tray_icon is not None:
                self.tray_icon.stop()
        except Exception:
            pass
        self.stop_event.set()
        try:
            self.root.after(100, self.root.destroy)
        except Exception:
            # Mainloop evtl. schon tot – hart beenden.
            os._exit(0)

    def request_quit(self):
        """Thread-sichere Anforderung zum Beenden.

        Aus Worker-Threads aufrufen – das Event wird im Hauptthread
        über process_ui_queue verarbeitet.
        """
        global SHUTTING_DOWN_FOR_UPDATE
        SHUTTING_DOWN_FOR_UPDATE = True
        try:
            self.ui_queue.put(("quit",))
        except Exception:
            os._exit(0)

    # --------------------------------------------------------
    # ANRUFERKENNUNG
    # --------------------------------------------------------

    def set_call_display(self, name, number="", result=""):
        self.call_name.config(text=name)
        self.call_number.config(
            text=f"Nummer: {number}" if number else ""
        )
        self.call_result.config(text=result)

    def on_toast_name_edit(self, number, name, kontakt=None):
        """Vom Toast: bearbeiteten Namen/Kontakt im Telefonbuch speichern."""
        if not number:
            return

        contact = get_contact(number)
        final_name = name or (contact["name"] if contact else None)
        if not final_name:
            return
        if kontakt is None:
            kontakt = (
                contact["kontakt"]
                if contact and "kontakt" in contact.keys()
                else ""
            )

        # Auch im laufenden Anrufprotokoll Namen/Kontakt nachziehen.
        if (
            self.call_started_at is not None
            and normalize_phone(number)
            == normalize_phone(self.call_log_number)
        ):
            self.call_log_name = final_name
            self.call_log_kontakt = kontakt or ""

        save_contact(number, final_name, kontakt=kontakt or "", source="Manuell")
        self.refresh_lists()
        logger.info("Name/Kontakt aus Toast gespeichert: %s → %s / %s", number, final_name, kontakt)

    def _log_call_end(self):
        """Speichert den beendeten Anruf im Anrufprotokoll."""
        if self.call_started_at is None:
            return

        duration_s = max(
            0,
            int((datetime.now() - self.call_started_at).total_seconds()),
        )

        try:
            save_call_log(
                self.call_log_number,
                self.call_log_name,
                self.call_log_kontakt,
                self.call_started_at.strftime("%Y-%m-%d %H:%M:%S"),
                duration_s,
            )
            self.refresh_call_log()
            print(f"TRACE: Anruf protokolliert: {self.call_log_number} "
                  f"({self.call_log_name} / {self.call_log_kontakt}), {duration_s} s")
        except Exception as error:
            print(f"Fehler beim Protokollieren: {error}")

        self.call_started_at = None
        self.call_log_kontakt = ""

    def process_call(self, call):
        logger.info(f"TRACE: process_call aufgerufen: call={call}, test_mode={getattr(self, 'test_mode', 'N/A')}")

        if call is None:
            # Im Testmodus beendet nur end_test_call() den Anruf –
            # einzelne Leer-Polls dürfen den Toast nicht killen.
            if self.test_mode:
                return
            if self.last_call_id is None:
                self._none_count = 0
                return
            # Erst nach 2x None in Folge gilt der Anruf als beendet
            # (Schutz vor einzelnen Lese-Aussetzern der Swisscom-App).
            self._none_count += 1
            if self._none_count < 2:
                return
            self._none_count = 0
            # Anruf nicht mehr aktiv: Anzeige und Toast ausblenden.
            logger.info("TRACE: process_call(None) – Anruf beendet, blende Toast aus")
            # Zuerst Toast schliessen (committet offene Bearbeitungen),
            # dann protokollieren, damit Name/Kontakt im Log landen.
            self.toast.hide()
            self._log_call_end()
            self.set_call_display("Warte auf eingehenden Anruf ...")
            logger.info("TRACE: toast.hide() ausgeführt")

            self.last_call_id = None
            return

        name = call["name"]
        number = call["nummer"]
        status = call.get("status")
        normalized = normalize_phone(number)
        call_id = (normalized, name)
        self._none_count = 0

        if call_id == self.last_call_id:
            # Gleicher Anruf, evtl. neuer Status (Klingeln -> Verbunden):
            # Toast stehen lassen, nur Titel aktualisieren.
            if status:
                self.toast.update_status(status)
            return

        # Vorherigen Anruf zu Ende protokollieren, falls direkt
        # ein neuer beginnt (ohne None-Zwischenpoll).
        if self.last_call_id is not None:
            self._log_call_end()

        self.last_call_id = call_id
        self.call_started_at = datetime.now()
        self.call_log_name = name or "Unbekannter Anrufer"
        self.call_log_kontakt = ""
        self.call_log_number = number

        # 1. Lokales Telefonbuch hat Vorrang.
        contact = get_contact(number)

        if contact:
            self.call_log_name = contact["name"]
            self.call_log_kontakt = (
                contact["kontakt"] if "kontakt" in contact.keys() else ""
            )
            self.set_call_display(
                contact["name"],
                number,
                "Aus dem lokalen Telefonbuch geladen.",
            )
            self.toast.show(contact["name"], number, status, self.call_log_kontakt)
            return

        # 2. Interne Durchwahlen nicht online suchen.
        if not is_external_number(number):
            self.set_call_display(
                name,
                number,
                "Interne Nummer – keine Online-Suche.",
            )

            if is_real_name(name):
                save_contact(number, name, source="Swisscom")
                self.refresh_lists()

            self.toast.show(name, number, status)
            return

        # 3. Bereits unbekannte Nummern nicht automatisch erneut suchen.
        if is_unknown(number):
            entry = get_unknown_entry(number)
            reason = (
                entry["reason"]
                if entry
                else "Bereits als unbekannt gespeichert."
            )

            self.set_call_display(
                name,
                number,
                f"{reason}\n"
                "Bereits in der Liste unbekannter Nummern.",
            )
            self.toast.show(name, number, status)
            return

        # 4. Angezeigten echten Namen von Swisscom übernehmen.
        # Ausnahme: Namen mit 'verteiler' müssen zwingend online nachgeschlagen werden,
        # da Verteiler weitergeleitet werden und der eigentliche Anrufer dahintersteckt.
        if is_real_name(name) and not _is_verteiler(name):
            self.call_log_name = name
            save_contact(number, name, source="Swisscom")
            self.set_call_display(
                name,
                number,
                "Name von Swisscom übernommen und lokal gespeichert.",
            )
            self.refresh_lists()
            self.toast.show(name, number, status)
            return

        # 5. Unbekannte externe Nummer online suchen.
        # Im Test-Modus: Toast sofort anzeigen, keine Online-Suche.
        if self.test_mode:
            self.toast.show(name, number, status)
            return
        self.toast.show(name, number, status)
        self.start_lookup(number, name)

    # --------------------------------------------------------
    # ONLINE-SUCHE
    # --------------------------------------------------------

    def start_lookup(self, number, swisscom_name):
        normalized = normalize_phone(number)

        if normalized in self.pending:
            return

        self.pending.add(normalized)
        allow_suffix_search = self.durchwahl_suche.get()

        self.set_call_display(
            swisscom_name,
            number,
            "Suche im Online-Telefonverzeichnis ...",
        )

        threading.Thread(
            target=self.lookup_worker,
            args=(
                number,
                swisscom_name,
                normalized,
                allow_suffix_search,
            ),
            daemon=True,
        ).start()

    def lookup_worker(
        self,
        number,
        swisscom_name,
        normalized,
        allow_suffix_search,
    ):
        result = reverse_lookup(number, allow_suffix_search)

        self.ui_queue.put((
            "lookup_done",
            number,
            swisscom_name,
            normalized,
            result,
        ))

    def finish_lookup(
        self,
        number,
        swisscom_name,
        normalized,
        result,
    ):
        self.pending.discard(normalized)

        # Ein älteres Suchergebnis darf keine neuere Anrufanzeige
        # überschreiben.
        if self.last_call_id is not None:
            if self.last_call_id[0] != normalized:
                pass

        status, data, message = result

        if status == "found":
            save_contact(number, data, source="search.ch")
            # Nur Treffer kosten Kontingent.
            try:
                decrement_api_quota()
            except Exception as error:
                logger.warning("API-Kontingent konnte nicht aktualisiert werden: %s", error)

            if (
                self.last_call_id is not None
                and self.last_call_id[0] == normalized
            ):
                self.call_log_name = data
                self.set_call_display(
                    data,
                    number,
                    "Eindeutiger Treffer. Im Telefonbuch gespeichert.",
                )
                self.toast.update_name(data)

        elif status == "candidates":
            add_unknown(number, message)

            if (
                self.last_call_id is not None
                and self.last_call_id[0] == normalized
            ):
                self.set_call_display(swisscom_name, number, message)

            self.show_candidates(number, data)

        else:
            # Ausnahme Verteiler bei fehlgeschlagener Suche: Nicht als
            # unbekannt speichern, damit der nächste Anruf es erneut
            # versucht (API dann evtl. wieder verfügbar). Der
            # Swisscom-Name wird einfach übernommen/angezeigt.
            skip_unknown = (
                status == "error" and _is_verteiler(swisscom_name)
            )
            if not skip_unknown:
                add_unknown(number, message)

            if (
                self.last_call_id is not None
                and self.last_call_id[0] == normalized
            ):
                if skip_unknown:
                    self.set_call_display(
                        swisscom_name,
                        number,
                        f"{message}\n"
                        "Wird beim nächsten Anruf erneut geprüft.",
                    )
                else:
                    self.set_call_display(
                        swisscom_name,
                        number,
                        f"{message}\n"
                        "Nummer in der Liste unbekannter Nummern gespeichert.",
                    )

        self.refresh_lists()
        self.refresh_api_quota()

    # --------------------------------------------------------
    # DURCHWAHL-TREFFER MANUELL ÜBERNEHMEN
    # --------------------------------------------------------

    def show_candidates(self, original_number, candidates):
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Durchwahl-Treffer für {original_number}")
        dialog.geometry("680x410")
        dialog.transient(self.root)
        set_window_icon(dialog)

        ttk.Label(
            dialog,
            text=(
                f"Kein exakter Treffer für {original_number}.\n"
                "Bitte einen passenden Verzeichniseintrag auswählen."
            ),
            wraplength=640,
        ).pack(padx=12, pady=12)

        tree = ttk.Treeview(
            dialog,
            columns=("number", "name"),
            show="headings",
            selectmode="browse",
        )

        tree.heading("number", text="Gefundene Nummer")
        tree.heading("name", text="Name")
        tree.column("number", width=190)
        tree.column("name", width=430)
        tree.pack(fill="both", expand=True, padx=12, pady=5)

        for index, (candidate_number, candidate_name) in enumerate(
            candidates
        ):
            tree.insert(
                "",
                "end",
                iid=str(index),
                values=(candidate_number, candidate_name),
            )

        def accept_candidate():
            selected = tree.selection()

            if not selected:
                messagebox.showinfo(
                    "Auswahl fehlt",
                    "Bitte zuerst einen Treffer auswählen.",
                    parent=dialog,
                )
                return

            candidate_number, candidate_name = tree.item(
                selected[0], "values"
            )

            confirmed = messagebox.askyesno(
                "Kontakt übernehmen",
                (
                    f"Den Namen '{candidate_name}' der tatsächlich "
                    f"eingehenden Nummer {original_number} zuordnen?\n\n"
                    f"Verzeichniseintrag: {candidate_number}"
                ),
                parent=dialog,
            )

            if not confirmed:
                return

            # Absichtlich die eingehende Nummer speichern,
            # nicht die ähnliche Nummer des Verzeichniseintrags.
            save_contact(
                original_number,
                candidate_name,
                source="search.ch Durchwahl-Suche",
            )

            self.current_unknown_number = None
            self.refresh_lists()

            if (
                self.last_call_id is not None
                and self.last_call_id[0] == normalize_phone(original_number)
            ):
                self.set_call_display(
                    candidate_name,
                    original_number,
                    f"Manuell bestätigter Treffer: {candidate_number}",
                )

            dialog.destroy()

        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=12, pady=12)

        ttk.Button(
            buttons,
            text="Ausgewählten Namen übernehmen",
            command=accept_candidate,
        ).pack(side="left")

        ttk.Button(
            buttons,
            text="Schliessen",
            command=dialog.destroy,
        ).pack(side="right")

    # --------------------------------------------------------
    # TELEFONBUCH BEARBEITEN
    # --------------------------------------------------------

    def on_contact_selected(self, _event=None):
        selected = self.book_tree.selection()

        if not selected:
            return

        original_number = selected[0]
        contact = get_contact(original_number)

        if not contact:
            return

        self.edit_original_number = original_number
        self.edit_number_var.set(contact["display_number"])
        self.edit_name_var.set(contact["name"])
        self.edit_kontakt_var.set(
            contact["kontakt"] if "kontakt" in contact.keys() else ""
        )

    def update_selected_contact(self):
        original_number = self.edit_original_number
        new_number = self.edit_number_var.get().strip()
        new_name = self.edit_name_var.get().strip()
        new_kontakt = self.edit_kontakt_var.get().strip()

        if not original_number:
            messagebox.showinfo(
                "Keine Auswahl",
                "Bitte zuerst einen Kontakt im Telefonbuch auswählen.",
            )
            return

        if not normalize_phone(new_number):
            messagebox.showwarning(
                "Ungültige Nummer",
                "Bitte eine gültige Telefonnummer eingeben.",
            )
            return

        if not new_name:
            messagebox.showwarning(
                "Name fehlt",
                "Bitte einen Namen eingeben.",
            )
            return

        old_normalized = normalize_phone(original_number)
        new_normalized = normalize_phone(new_number)

        existing = get_contact(new_number)

        if existing and new_normalized != old_normalized:
            messagebox.showwarning(
                "Nummer bereits vorhanden",
                "Diese Telefonnummer ist bereits im Telefonbuch gespeichert.",
            )
            return

        try:
            # Zuerst den neuen Eintrag schreiben.
            save_contact(
                new_number,
                new_name,
                kontakt=new_kontakt,
                source="Manuell",
            )

            # Falls sich die Nummer geändert hat, alten Eintrag löschen.
            if new_normalized != old_normalized:
                delete_contact(old_normalized)

            self.edit_original_number = new_normalized
            self.refresh_lists()

            if self.book_tree.exists(new_normalized):
                self.book_tree.selection_set(new_normalized)
                self.book_tree.focus(new_normalized)
                self.book_tree.see(new_normalized)

            self.edit_number_var.set(new_number)
            self.edit_name_var.set(new_name)
            self.edit_kontakt_var.set(new_kontakt)

            messagebox.showinfo(
                "Gespeichert",
                "Der Kontakt wurde aktualisiert.",
            )

        except Exception as error:
            messagebox.showerror(
                "Fehler",
                f"Der Kontakt konnte nicht gespeichert werden:\n{error}",
            )

    def delete_selected_contact(self):
        selected = self.book_tree.selection()

        if not selected:
            messagebox.showinfo(
                "Keine Auswahl",
                "Bitte zuerst einen Kontakt auswählen.",
            )
            return

        row = get_contact(selected[0])

        if not row:
            return

        if not messagebox.askyesno(
            "Kontakt löschen",
            f"{row['name']} ({row['display_number']}) löschen?",
        ):
            return

        delete_contact(selected[0])

        self.edit_original_number = None
        self.edit_number_var.set("")
        self.edit_name_var.set("")
        self.edit_kontakt_var.set("")
        self.refresh_lists()

    # --------------------------------------------------------
    # TELEFONBUCH EXPORT / IMPORT (CSV)
    # --------------------------------------------------------

    def export_phonebook(self):
        contacts = get_all_contacts()

        if not contacts:
            messagebox.showinfo(
                "Export",
                "Das Telefonbuch ist leer – nichts zu exportieren.",
            )
            return

        path = filedialog.asksaveasfilename(
            title="Telefonbuch exportieren",
            defaultextension=".csv",
            filetypes=[("CSV-Datei", "*.csv")],
            initialfile="telefonbuch_export.csv",
        )

        if not path:
            return

        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f, delimiter=";")
                writer.writerow(["Nummer", "Name", "Quelle"])
                for row in contacts:
                    # ="..." schützt die Nummer vor Excel,
                    # das sonst das führende + entfernt.
                    num = row["display_number"]
                    if num and num[0] in "+0":
                        num = f'="{num}"'
                    writer.writerow(
                        [num, row["name"], row["source"]]
                    )
        except OSError as e:
            messagebox.showerror("Export fehlgeschlagen", str(e))
            return

        messagebox.showinfo(
            "Export abgeschlossen",
            f"{len(contacts)} Kontakte exportiert nach:\n{path}",
        )

    def import_phonebook(self):
        path = filedialog.askopenfilename(
            title="Telefonbuch importieren",
            filetypes=[("CSV-Datei", "*.csv"), ("Alle Dateien", "*.*")],
        )

        if not path:
            return

        try:
            with open(path, "r", newline="", encoding="utf-8-sig") as f:
                reader = csv.reader(f, delimiter=";")
                header = next(reader, None)

                # Header optional überspringen
                if header and "nummer" not in (header[0] or "").lower():
                    # Erste Zeile war keine Header-Zeile → als Daten behandeln
                    rows = [header] + list(reader)
                else:
                    rows = list(reader)
        except (OSError, csv.Error) as e:
            messagebox.showerror("Import fehlgeschlagen", str(e))
            return

        imported = 0
        skipped = 0

        for row in rows:
            if len(row) < 2:
                continue

            number = (row[0] or "").strip()
            name = (row[1] or "").strip()
            source = (row[2].strip() if len(row) > 2 and row[2] else "Import")

            # Excel-Schutz ="..." wieder entfernen.
            if number.startswith('="') and number.endswith('"'):
                number = number[2:-1]

            if not number or not name:
                skipped += 1
                continue

            try:
                save_contact(number, name, source=source)
                imported += 1
            except ValueError:
                skipped += 1

        self.refresh_lists()

        messagebox.showinfo(
            "Import abgeschlossen",
            f"{imported} Kontakte importiert.\n{skipped} Zeilen übersprungen.",
        )

    # --------------------------------------------------------
    # UNBEKANNTE NUMMERN BEARBEITEN
    # --------------------------------------------------------

    def on_unknown_selected(self, _event=None):
        selected = self.unknown_tree.selection()

        if not selected:
            return

        normalized = selected[0]
        entry = get_unknown_entry(normalized)

        if not entry:
            return

        self.current_unknown_number = normalized
        self.unknown_number_var.set(entry["display_number"])
        self.unknown_name_var.set("")
        self.unknown_kontakt_var.set(
            entry["kontakt"] if "kontakt" in entry.keys() else ""
        )

    def save_unknown_as_contact(self):
        number = self.unknown_number_var.get().strip()
        name = self.unknown_name_var.get().strip()
        kontakt = self.unknown_kontakt_var.get().strip()

        if not number:
            messagebox.showinfo(
                "Keine Auswahl",
                "Bitte zuerst eine unbekannte Nummer auswählen.",
            )
            return

        if not name:
            messagebox.showwarning(
                "Name fehlt",
                "Bitte einen Namen eingeben.",
            )
            return

        try:
            save_contact(number, name, kontakt=kontakt, source="Manuell")
            self.current_unknown_number = None
            self.refresh_lists()

            self.set_call_display(
                name,
                number,
                "Kontakt im lokalen Telefonbuch gespeichert.",
            )

        except Exception as error:
            messagebox.showerror("Fehler", str(error))

    def delete_selected_unknown(self):
        number = self.unknown_number_var.get().strip()

        if not number:
            messagebox.showinfo(
                "Keine Auswahl",
                "Bitte zuerst eine unbekannte Nummer auswählen.",
            )
            return

        if not messagebox.askyesno(
            "Nummer löschen",
            f"Unbekannte Nummer {number} wirklich löschen?",
        ):
            return

        delete_unknown(number)

        self.current_unknown_number = None
        self.unknown_number_var.set("")
        self.unknown_name_var.set("")
        self.unknown_kontakt_var.set("")
        self.refresh_lists()

    def retry_selected_lookup(self):
        number = self.unknown_number_var.get().strip()

        if not number:
            messagebox.showinfo(
                "Keine Auswahl",
                "Bitte zuerst eine unbekannte Nummer auswählen.",
            )
            return

        delete_unknown(number)
        self.refresh_lists()
        self.start_lookup(number, "Unbekannter Anrufer")

    # --------------------------------------------------------
    # TABELLEN AKTUALISIEREN
    # --------------------------------------------------------

    def refresh_api_quota(self):
        """Aktualisiert die Kontingent-Anzeige (Hauptthread)."""
        try:
            remaining = get_api_quota()
        except Exception:
            remaining = API_QUOTA_MONTHLY
        self.api_quota_var.set(
            f"Noch verfügbare API-Abfragen diesen Monat: {remaining}"
        )

    def refresh_lists(self):
        selected_contact = self.edit_original_number

        for item in self.book_tree.get_children():
            self.book_tree.delete(item)

        for row in get_all_contacts():
            self.book_tree.insert(
                "",
                "end",
                iid=row["number"],
                values=(
                    row["display_number"],
                    row["name"],
                    row["kontakt"] if "kontakt" in row.keys() else "",
                    row["source"],
                ),
            )

        # Auswahl und Bearbeitungsfelder möglichst beibehalten.
        if selected_contact and self.book_tree.exists(selected_contact):
            self.book_tree.selection_set(selected_contact)
            self.book_tree.focus(selected_contact)

        for item in self.unknown_tree.get_children():
            self.unknown_tree.delete(item)

        for row in get_all_unknown():
            self.unknown_tree.insert(
                "",
                "end",
                iid=row["number"],
                values=(
                    row["display_number"],
                    row["kontakt"] if "kontakt" in row.keys() else "",
                    row["last_seen"],
                    row["reason"],
                ),
            )

        self.refresh_call_log()

    def refresh_call_log(self):
        """Aktualisiert das Anrufprotokoll und wendet die aktuelle Sortierung an."""
        for item in self.log_tree.get_children():
            self.log_tree.delete(item)

        rows = list(get_call_logs())
        rows.sort(key=self._log_sort_key, reverse=self._log_sort_desc)

        for row in rows:
            started = row["started_at"]
            # Datum: dd.mm.jjjj
            date_part = started[8:10] + "." + started[5:7] + "." + started[0:4] if len(started) >= 10 else started
            # Zeit: hh:mm
            time_part = started[11:16] if len(started) >= 16 else ""

            minutes, seconds = divmod(row["duration_s"], 60)
            hours, minutes = divmod(minutes, 60)

            if hours:
                duration_text = f"{hours * 60 + minutes}:{seconds:02d}"
            else:
                duration_text = f"{minutes}:{seconds:02d}"

            self.log_tree.insert(
                "",
                "end",
                values=(
                    date_part,      # dd.mm.jjjj
                    time_part,      # hh:mm
                    row["display_number"],
                    row["name"],
                    row["kontakt"] if "kontakt" in row.keys() else "",
                    duration_text,
                ),
            )

    def _log_sort_key(self, row):
        """Sortierschlüssel für die angegebene Spalte."""
        col = self._log_sort_column
        if col == "date":
            return row["started_at"]
        if col == "time":
            return row["started_at"]
        if col == "number":
            return row["display_number"] or ""
        if col == "name":
            return (row["name"] or "").lower()
        if col == "kontakt":
            return ((row["kontakt"] if "kontakt" in row.keys() else "") or "").lower()
        if col == "duration":
            return row["duration_s"]
        return row["started_at"]

    def _sort_log_tree(self, column):
        """Kopfklick: Spalte sortieren, Richtung umschalten."""
        if self._log_sort_column == column:
            self._log_sort_desc = not self._log_sort_desc
        else:
            self._log_sort_column = column
            # Standard: Datum/Zeit absteigend (aktueller Anruf oben),
            # sonst aufsteigend.
            self._log_sort_desc = column in ("date", "time")
        self.refresh_call_log()


# ============================================================
# MIGRATION & ERSTSTART
# ============================================================

LEGACY_DB_FILE = Path(__file__).resolve().with_name("telefonbuch.db")


def migrate_legacy_db():
    """Verschiebt eine alte DB (neben der .py) ins AppData-Verzeichnis."""
    try:
        if LEGACY_DB_FILE.exists() and LEGACY_DB_FILE != DB_FILE:
            if not DB_FILE.exists():
                shutil.copy2(LEGACY_DB_FILE, DB_FILE)
                logger.info("Alte DB nach AppData migriert: %s", DB_FILE)
            else:
                logger.info(
                    "Alte DB %s vorhanden, aber neue DB %s existiert bereits – "
                    "Migration übersprungen.",
                    LEGACY_DB_FILE, DB_FILE,
                )
    except OSError as e:
        logger.error("DB-Migration fehlgeschlagen: %s", e)


def first_run_csv_import():
    """Fragt beim allerersten Start, ob eine CSV importiert werden soll."""
    if DB_FILE.exists():
        return

    root = tk.Tk()
    root.withdraw()
    set_window_icon(root)

    if messagebox.askyesno(
        "Erster Start",
        "Willkommen! Es wurde noch keine Datenbank gefunden.\n\n"
        "Möchten Sie jetzt ein Telefonbuch aus einer CSV-Datei importieren?\n"
        "(Spalten: Nummer;Name;Quelle)",
    ):
        path = filedialog.askopenfilename(
            title="Telefonbuch-CSV auswählen",
            filetypes=[("CSV-Datei", "*.csv"), ("Alle Dateien", "*.*")],
        )

        if path:
            try:
                with open(path, "r", newline="", encoding="utf-8-sig") as f:
                    reader = csv.reader(f, delimiter=";")
                    header = next(reader, None)
                    if header and "nummer" not in (header[0] or "").lower():
                        rows = [header] + list(reader)
                    else:
                        rows = list(reader)

                imported = 0
                for row in rows:
                    if len(row) < 2:
                        continue
                    number = (row[0] or "").strip()
                    name = (row[1] or "").strip()
                    source = (
                        row[2].strip()
                        if len(row) > 2 and row[2]
                        else "Import"
                    )
                    # Excel-Schutz ="..." wieder entfernen.
                    if number.startswith('="') and number.endswith('"'):
                        number = number[2:-1]
                    if not number or not name:
                        continue
                    try:
                        save_contact(number, name, source=source)
                        imported += 1
                    except ValueError:
                        pass

                messagebox.showinfo(
                    "Import abgeschlossen",
                    f"{imported} Kontakte importiert.",
                )
            except (OSError, csv.Error) as e:
                messagebox.showerror("Import fehlgeschlagen", str(e))

    root.destroy()


# ============================================================
# UPDATE-CHECK
# ============================================================

def parse_version(text):
    """Extrahiert eine vergleichbare Versionstupel aus 'v1.2.3' o.ä."""
    numbers = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in numbers[:3]) or (0,)


def check_for_update():
    """Prüft GitHub nach einer neueren Version.

    Gibt (version, installer_url) zurück oder None.
    """
    if not GITHUB_REPO:
        return None

    try:
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.warning("Update-Check fehlgeschlagen: %s", e)
        return None

    tag = (data.get("tag_name") or "").lstrip("v")
    if parse_version(tag) <= parse_version(APP_VERSION):
        return None

    # Installer-Asset suchen (Telefonerkennung-Setup-<version>.exe).
    installer_url = None
    expected_name = f"Telefonerkennung-Setup-{tag}.exe"
    for asset in data.get("assets", []):
        if asset.get("name") == expected_name:
            installer_url = asset.get("browser_download_url")
            break

    if not installer_url:
        logger.warning("Update %s gefunden, aber kein Installer-Asset (%s).", tag, expected_name)
        return None

    return tag, installer_url


def download_and_install_update(installer_url, version):
    """Lädt den Installer herunter und startet ihn.
    
    Der Installer macht alles: App beenden, Dateien ersetzen, Neustart.
    """
    global SHUTTING_DOWN_FOR_UPDATE
    try:
        resp = requests.get(installer_url, stream=True, timeout=120)
        resp.raise_for_status()

        suffix = ".exe"
        installer_path = Path(tempfile.gettempdir()) / f"Telefonerkennung-Setup-{version}{suffix}"

        with open(installer_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        logger.info("Installer heruntergeladen: %s", installer_path)

        # Flag setzen: on_close beendet die App wirklich
        global SHUTTING_DOWN_FOR_UPDATE
        SHUTTING_DOWN_FOR_UPDATE = True

        # Installer starten (macht alles: App beenden, Dateien ersetzen, Neustart)
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
        )
        subprocess.Popen(
            [str(installer_path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            creationflags=creationflags
        )

        logger.info("Installer gestartet, beende App sauber.")

        if _app_instance is not None:
            _app_instance.request_quit()
        else:
            os._exit(0)

        threading.Timer(10, os._exit, args=(0,)).start()

    except Exception as e:
        logger.error("Update-Installation fehlgeschlagen: %s", e)
        SHUTTING_DOWN_FOR_UPDATE = False
        return False


# ============================================================
# SINGLE INSTANCE
# ============================================================

# Handle des benannten Mutex – muss lebend bleiben, sonst wird er
# freigegeben und eine zweite Instanz könnte ihn erneut anlegen.
_single_instance_mutex = None


def _acquire_single_instance_lock(timeout=10.0):
    """Versucht, den Single-Instance-Mutex zu bekommen.

    Läuft bereits eine Instanz, wird so lange gewartet, bis sie von
    selbst beendet ist (max. `timeout` Sekunden). Das deckt den
    Update-Fall ab: die alte Instanz ist gerade im Begriff zu
    sterben, die neue übernimmt. Nach Ablauf gibt es auf – die
    zweite Instanz startet dann nicht.

    Gibt True zurück, wenn diese Instanz weiterlaufen darf.
    """
    global _single_instance_mutex

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    ERROR_ALREADY_EXISTS = 183
    ERROR_ACCESS_DENIED = 5

    deadline = time.time() + timeout
    while True:
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(
            None, False, "TelefonerkennungSingleInstance"
        )
        last_error = ctypes.get_last_error()

        if handle:
            if last_error != ERROR_ALREADY_EXISTS:
                # Wir haben den Mutex: wir sind die einzige Instanz.
                _single_instance_mutex = handle
                return True
            # Andere Instanz im gleichen Integrity-Level: Handle wieder
            # freigeben und warten. Ohne CloseHandle bliebe der Mutex
            # referenziert und würde erst beim Prozessende frei.
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
        elif last_error == ERROR_ACCESS_DENIED:
            # Eine ERHÖHTE Instanz (Admin) hält den Mutex, wir laufen
            # normal und bekommen keinen Zugriff. Auch das heißt:
            # jemand anders läuft – also warten.
            logger.debug(
                "Mutex von erhöhter Instanz gehalten (LastError=%s).",
                last_error,
            )
        else:
            # Anderer Fehler (z.B. ungültiger Name): Single-Instance
            # können wir hier nicht erzwingen. Nicht blockieren, sonst
            # startet die App gar nicht mehr.
            logger.warning(
                "Mutex erstellbar (LastError=%s) – Single-Instance deaktiviert.",
                last_error,
            )
            return True

        if time.time() >= deadline:
            return False

        time.sleep(0.1)


def ensure_single_instance():
    """Erzwingt, dass nur eine Instanz läuft.

    Die zweite Instanz wartet kurz auf die erste (Update-Fall) und
    beendet sich dann still – ohne Rückmeldung und ohne die laufende
    Instanz zu stören.
    """
    if _acquire_single_instance_lock():
        return
    logger.info(
        "Weitere Instanz bereits aktiv (PID %s) – starte nicht erneut.",
        os.getpid(),
    )
    os._exit(0)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    ensure_single_instance()

    migrate_legacy_db()
    init_db()
    migrate_add_kontakt()
    cleanup_old_call_logs()
    first_run_csv_import()
    init_db()  # Erneut nach möglichem CSV-Import.

    root = tk.Tk()
    app = CallerIDApp(root)
    _app_instance = app

    # Update-Check im Hintergrund; GUI-Dialoge im Hauptthread via after().
    def _update_check_worker():
        result = check_for_update()
        if not result:
            return
        version, url = result
        logger.info("Update verfügbar: %s", version)

        def _show_and_install():
            messagebox.showinfo(
                "Update",
                f"Eine neue Version ({version}) ist verfügbar.\n"
                "Das Update wird jetzt heruntergeladen und installiert.\n"
                "Die App wird danach beendet und neu gestartet.",
            )
            # Erst nach OK den Download starten, damit der
            # taskkill den Prozess nicht vorzeitig killt.
            def _install_worker():
                download_and_install_update(url, version)

            threading.Thread(target=_install_worker, daemon=True).start()

        root.after(0, _show_and_install)

    if GITHUB_REPO:
        threading.Thread(target=_update_check_worker, daemon=True).start()

    root.mainloop()
