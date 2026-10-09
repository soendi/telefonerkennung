
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import xml.etree.ElementTree as ET

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

APP_VERSION = "1.0.2"

# GitHub-Repo für Update-Checks (owner/rename), z.B. "maxmuster/telefonerkennung".
# Leer lassen deaktiviert den Update-Check.
GITHUB_REPO = "soendi/telefonerkennung"

SEARCH_CH_API_KEY = "8b09c242997465ed4ca0e9176c103767"
SEARCH_CH_API_URL = "https://search.ch/tel/api/"

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


# ============================================================
# LOGGING
# ============================================================

def setup_logging():
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
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS unknown_numbers (
                number TEXT PRIMARY KEY,
                display_number TEXT NOT NULL,
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
                started_at TEXT NOT NULL,
                duration_s INTEGER NOT NULL
            )
        """)


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


def save_contact(number, name, source="Manuell"):
    normalized = normalize_phone(number)
    name = (name or "").strip()
    number = (number or "").strip()

    if not normalized:
        raise ValueError("Bitte eine gültige Telefonnummer eingeben.")

    if not name:
        raise ValueError("Der Name darf nicht leer sein.")

    with connect_db() as db:
        db.execute("""
            INSERT INTO phonebook
                (number, display_number, name, source, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(number) DO UPDATE SET
                display_number = excluded.display_number,
                name = excluded.name,
                source = excluded.source,
                updated_at = excluded.updated_at
        """, (normalized, number, name, source, now()))

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
            SELECT number, display_number, name, source, updated_at
            FROM phonebook
            ORDER BY name COLLATE NOCASE
        """).fetchall()


def get_all_unknown():
    with connect_db() as db:
        return db.execute("""
            SELECT number, display_number, first_seen, last_seen, reason
            FROM unknown_numbers
            ORDER BY last_seen DESC
        """).fetchall()


def save_call_log(number, name, started_at, duration_s):
    with connect_db() as db:
        db.execute("""
            INSERT INTO call_log
                (number, display_number, name, started_at, duration_s)
            VALUES (?, ?, ?, ?, ?)
        """, (
            normalize_phone(number),
            number,
            name or "Unbekannter Anrufer",
            started_at,
            duration_s,
        ))


def get_call_logs(limit=500):
    with connect_db() as db:
        return db.execute("""
            SELECT display_number, name, started_at, duration_s
            FROM call_log
            ORDER BY id DESC
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
# TOAST-FENSTER
# ============================================================

class CallToast:
    def __init__(self, root):
        self.root = root
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
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

        for widget in (frame, content, self.title_label,
                       self.number_label):
            widget.bind("<Button-1>", self._on_click)

        # Doppelklick auf den Namen: inline bearbeiten.
        self.name_label.bind("<Double-Button-1>", self._start_edit)
        self.name_entry.bind("<Return>", self._commit_edit)
        self.name_entry.bind("<Escape>", self._cancel_edit)
        self.name_entry.bind("<FocusOut>", self._commit_edit)

    def set_click_callback(self, callback):
        self._click_callback = callback

    def set_edit_callback(self, callback):
        """callback(number, name) – wird beim Ausblenden mit
        geändertem Namen aufgerufen."""
        self._edit_callback = callback

    def _on_click(self, _event=None):
        if self._editing:
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
        self.name_label.pack(fill="x", before=self.number_label)

        if new_name and new_name != self._original_name:
            self._edited_name = new_name
            self.name_label.config(text=new_name)
        elif new_name:
            self.name_label.config(text=new_name)

    def _cancel_edit(self, _event=None):
        self._editing = False
        self.name_entry.pack_forget()
        self.name_label.pack(fill="x", before=self.number_label)
        return "break"

    def show(self, name, number):
        # Restlichen Editierzustand zurücksetzen.
        if self._editing:
            self._cancel_edit()
        self._edited_name = None
        self._original_name = name or "Unbekannter Anrufer"
        self._current_number = number or ""

        self.name_label.config(text=self._original_name)
        self.number_label.config(text=format_phone_display(self._current_number))
        self._ring_seconds = 0
        self.title_label.config(text="Eingehender Anruf (0 s)")
        self._calc_target()
        self.window.deiconify()
        self.window.lift()
        self._start_ticker()
        self._animate_in()

    def _start_ticker(self):
        self._stop_ticker()
        self._tick()

    def _tick(self):
        self._ring_seconds += 1
        self.title_label.config(
            text=f"Eingehender Anruf ({self._ring_seconds} s)"
        )
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
        self._stop_ticker()

        # Editierten Namen speichern, bevor das Fenster schliesst.
        if self._editing:
            self._commit_edit()

        if (
            self._edited_name
            and self._current_number
            and self._edit_callback
        ):
            try:
                self._edit_callback(
                    self._current_number, self._edited_name
                )
            except Exception:
                pass
            self._edited_name = None

        self._animate_out()

    # ---------------------------------------------------------
    # SLIDE-ANIMATION
    # ---------------------------------------------------------

    ANIM_STEPS = 15
    ANIM_DELAY_MS = 15

    def _calc_target(self):
        """Berechnet die Zielposition: rechte Kante, auf Taskleiste."""
        self.window.update_idletasks()
        w = 400
        h = self.window.winfo_reqheight()
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
            ctypes.windll.user32.SystemParametersInfoW(
                0x0030, 0, ctypes.byref(rect), 0
            )
            work_bottom = rect.bottom
        except Exception:
            work_bottom = self.root.winfo_screenheight()

        self._target_x = screen_w - w
        self._target_y = work_bottom - h
        self._screen_w = screen_w

    def _stop_anim(self):
        if self._anim_after_id is not None:
            try:
                self.window.after_cancel(self._anim_after_id)
            except Exception:
                pass
            self._anim_after_id = None

    def _animate_in(self):
        """Fliegt von rechts (Bildschirmrand) nach links an den Zielort."""
        self._stop_anim()
        start_x = self._screen_w
        end_x = self._target_x
        y = self._target_y
        steps = self.ANIM_STEPS

        def step(i):
            # Ease-out: grosse Schritte am Anfang, kleine am Ende.
            progress = 1 - (1 - i / steps) ** 2
            x = int(start_x + (end_x - start_x) * progress)
            self.window.geometry(f"+{x}+{y}")
            if i < steps:
                self._anim_after_id = self.window.after(
                    self.ANIM_DELAY_MS, step, i + 1
                )
            else:
                self._anim_after_id = None

        step(0)

    def _animate_out(self):
        """Fliegt von links nach rechts zum Bildschirmrand hinaus."""
        self._stop_anim()
        self._calc_target()
        start_x = self._target_x
        end_x = self._screen_w
        y = self._target_y
        steps = self.ANIM_STEPS

        def step(i):
            # Ease-in: kleine Schritte am Anfang, grosse am Ende.
            progress = (i / steps) ** 2
            x = int(start_x + (end_x - start_x) * progress)
            self.window.geometry(f"+{x}+{y}")
            if i < steps:
                self._anim_after_id = self.window.after(
                    self.ANIM_DELAY_MS, step, i + 1
                )
            else:
                self._anim_after_id = None
                self.window.withdraw()

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

        if status != "Ankommender Anruf" or panel is None:
            return None

        texts = []

        for element in panel.descendants():
            try:
                if element.element_info.control_type == "Text":
                    value = element.window_text().strip()

                    if (
                        value
                        and value != "Ankommender Anruf"
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

        return {"name": name, "nummer": number}

    except Exception as error:
        print(f"Fehler beim Lesen der Swisscom-App: {error}")
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

        self.last_call_id = None
        self.pending = set()
        self.current_unknown_number = None

        # Anrufprotokoll: Startzeit und Anruferdaten des laufenden Anrufs.
        self.call_started_at = None
        self.call_log_name = ""
        self.call_log_number = ""

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

        # Fenster standardmaessig verstecken (wartet im Systray).
        self.root.withdraw()

        self.build_ui()
        self.refresh_lists()

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
        print("TEST: on_tray_test aufgerufen")
        self.ui_queue.put(("tray", "test"))

    def simulate_call(self):
        print(f"TEST: simulate_call aufgerufen, test_mode={self.test_mode}")
        if self.test_mode:
            return

        self.test_mode = True
        self.test_call_data = {
            "name": "Unbekannter Anrufer",
            "nummer": "+41817564572",
        }
        print(f"TEST: test_mode gesetzt, Daten={self.test_call_data}")

        # Testanruf nach 15 Sekunden automatisch beenden.
        self.root.after(15000, self.end_test_call)

    def end_test_call(self):
        print("TEST: end_test_call – Testanruf wird beendet (test_mode=False)")
        self.test_mode = False
        self.test_call_data = None

        # Toast direkt ausblenden (läuft im GUI-Thread via root.after).
        if self.last_call_id is not None:
            print("TRACE: blende Toast direkt nach Testende aus")
            self._log_call_end()
            self.set_call_display("Warte auf eingehenden Anruf ...")
            self.toast.hide()
            self.last_call_id = None
            print("TRACE: toast.hide() ausgeführt")

    def show_settings(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

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
            columns=("number", "name", "source"),
            show="headings",
            selectmode="browse",
        )

        self.book_tree.heading("number", text="Telefonnummer")
        self.book_tree.heading("name", text="Name")
        self.book_tree.heading("source", text="Quelle")

        self.book_tree.column("number", width=180)
        self.book_tree.column("name", width=330)
        self.book_tree.column("source", width=180)

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

        edit_contact_frame.columnconfigure(1, weight=1)

        contact_buttons = ttk.Frame(edit_contact_frame)
        contact_buttons.grid(
            row=2, column=0, columnspan=2,
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
            columns=("number", "seen", "reason"),
            show="headings",
            selectmode="browse",
            height=9,
        )

        self.unknown_tree.heading("number", text="Telefonnummer")
        self.unknown_tree.heading("seen", text="Zuletzt gesehen")
        self.unknown_tree.heading("reason", text="Suchergebnis")

        self.unknown_tree.column("number", width=150)
        self.unknown_tree.column("seen", width=155)
        self.unknown_tree.column("reason", width=400)

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

        edit_frame.columnconfigure(1, weight=1)

        unknown_buttons = ttk.Frame(edit_frame)
        unknown_buttons.grid(
            row=2, column=0, columnspan=2,
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

        self.log_tree = ttk.Treeview(
            log_frame,
            columns=("number", "name", "date", "duration"),
            show="headings",
            selectmode="browse",
        )

        self.log_tree.heading("number", text="Telefonnummer")
        self.log_tree.heading("name", text="Name")
        self.log_tree.heading("date", text="Datum")
        self.log_tree.heading("duration", text="Dauer")

        self.log_tree.column("number", width=160)
        self.log_tree.column("name", width=260)
        self.log_tree.column("date", width=160)
        self.log_tree.column("duration", width=80, anchor="e")

        log_scroll = ttk.Scrollbar(
            log_frame,
            orient="vertical",
            command=self.log_tree.yview,
        )
        self.log_tree.configure(yscrollcommand=log_scroll.set)

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
                    elif event[1] == "exit":
                        self.exit_app()
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

    def poll_worker(self):
        """Liest die Swisscom-Oberfläche ausserhalb des GUI-Threads."""
        while not self.stop_event.is_set():
            if self.test_mode:
                call = self.test_call_data
                print(f"TEST: poll_worker liefert Test-Call: {call}")
            else:
                call = get_incoming_call()
            self.ui_queue.put(("poll", call))
            self.stop_event.wait(POLL_INTERVAL_MS / 1000)

    def on_close(self):
        # Fenster nur verstecken – Programm laeuft im Systray weiter.
        self.root.withdraw()

    # --------------------------------------------------------
    # ANRUFERKENNUNG
    # --------------------------------------------------------

    def set_call_display(self, name, number="", result=""):
        self.call_name.config(text=name)
        self.call_number.config(
            text=f"Nummer: {number}" if number else ""
        )
        self.call_result.config(text=result)

    def on_toast_name_edit(self, number, name):
        """Vom Toast: bearbeiteten Namen im Telefonbuch speichern."""
        if not number or not name:
            return

        # Auch im laufenden Anrufprotokoll den Namen nachziehen.
        if (
            self.call_started_at is not None
            and normalize_phone(number)
            == normalize_phone(self.call_log_number)
        ):
            self.call_log_name = name

        save_contact(number, name, source="Manuell")
        self.refresh_lists()
        print(f"TRACE: Name aus Toast gespeichert: {number} → {name}")

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
                self.call_started_at.strftime("%Y-%m-%d %H:%M:%S"),
                duration_s,
            )
            self.refresh_call_log()
            print(f"TRACE: Anruf protokolliert: {self.call_log_number} "
                  f"({self.call_log_name}), {duration_s} s")
        except Exception as error:
            print(f"Fehler beim Protokollieren: {error}")

        self.call_started_at = None

    def process_call(self, call):
        print(f"TRACE: process_call: {call}")

        if call is None:
            if self.last_call_id is not None:
                # Anruf nicht mehr aktiv: Anzeige und Toast ausblenden.
                print("TRACE: process_call(None) – Anruf beendet, "
                      "blende Toast aus")
                self._log_call_end()
                self.set_call_display("Warte auf eingehenden Anruf ...")
                self.toast.hide()
                print("TRACE: toast.hide() ausgeführt")

            self.last_call_id = None
            return

        name = call["name"]
        number = call["nummer"]
        normalized = normalize_phone(number)
        call_id = (normalized, name)

        if call_id == self.last_call_id:
            return

        # Vorherigen Anruf zu Ende protokollieren, falls direkt
        # ein neuer beginnt (ohne None-Zwischenpoll).
        if self.last_call_id is not None:
            self._log_call_end()

        self.last_call_id = call_id
        self.call_started_at = datetime.now()
        self.call_log_name = name or "Unbekannter Anrufer"
        self.call_log_number = number

        # 1. Lokales Telefonbuch hat Vorrang.
        contact = get_contact(number)

        if contact:
            self.call_log_name = contact["name"]
            self.set_call_display(
                contact["name"],
                number,
                "Aus dem lokalen Telefonbuch geladen.",
            )
            self.toast.show(contact["name"], number)
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

            self.toast.show(name, number)
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
            self.toast.show(name, number)
            return

        # 4. Angezeigten echten Namen von Swisscom übernehmen.
        if is_real_name(name):
            self.call_log_name = name
            save_contact(number, name, source="Swisscom")
            self.set_call_display(
                name,
                number,
                "Name von Swisscom übernommen und lokal gespeichert.",
            )
            self.refresh_lists()
            self.toast.show(name, number)
            return

        # 5. Unbekannte externe Nummer online suchen.
        self.toast.show(name, number)
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
            add_unknown(number, message)

            if (
                self.last_call_id is not None
                and self.last_call_id[0] == normalized
            ):
                self.set_call_display(
                    swisscom_name,
                    number,
                    f"{message}\n"
                    "Nummer in der Liste unbekannter Nummern gespeichert.",
                )

        self.refresh_lists()

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

    def update_selected_contact(self):
        original_number = self.edit_original_number
        new_number = self.edit_number_var.get().strip()
        new_name = self.edit_name_var.get().strip()

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
                source="Manuell bearbeitet",
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
                    writer.writerow(
                        [row["display_number"], row["name"], row["source"]]
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

    def save_unknown_as_contact(self):
        number = self.unknown_number_var.get().strip()
        name = self.unknown_name_var.get().strip()

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
            save_contact(number, name, source="Manuell")
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
                    row["last_seen"],
                    row["reason"],
                ),
            )

        self.refresh_call_log()

    def refresh_call_log(self):
        for item in self.log_tree.get_children():
            self.log_tree.delete(item)

        for row in get_call_logs():
            minutes, seconds = divmod(row["duration_s"], 60)
            hours, minutes = divmod(minutes, 60)

            if hours:
                duration_text = f"{hours}:{minutes:02d}:{seconds:02d}"
            else:
                duration_text = f"{minutes}:{seconds:02d}"

            self.log_tree.insert(
                "",
                "end",
                values=(
                    row["display_number"],
                    row["name"],
                    row["started_at"],
                    duration_text,
                ),
            )


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

    # Passendes Installer-Asset suchen.
    installer_url = None
    for asset in data.get("assets", []):
        name = (asset.get("name") or "").lower()
        if name.endswith(".exe") or name.endswith(".msi"):
            installer_url = asset.get("browser_download_url")
            break

    if not installer_url:
        logger.warning("Update %s gefunden, aber kein Installer-Asset.", tag)
        return None

    return tag, installer_url


def download_and_install_update(url, version):
    """Lädt den Installer herunter und führt ihn aus."""
    try:
        resp = requests.get(url, stream=True, timeout=120)
        resp.raise_for_status()

        suffix = ".msi" if url.lower().endswith(".msi") else ".exe"
        installer_path = Path(tempfile.gettempdir()) / (
            f"telefonerkennung-setup-{version}{suffix}"
        )

        with open(installer_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        logger.info("Installer heruntergeladen: %s", installer_path)

        if suffix == ".msi":
            subprocess.Popen(
                ["msiexec", "/i", str(installer_path), "/qn", "/norestart"]
            )
        else:
            subprocess.Popen(
                [str(installer_path), "/SILENT", "/NORESTART"]
            )

        return True
    except (OSError, requests.RequestException) as e:
        logger.error("Update-Installation fehlgeschlagen: %s", e)
        return False


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    migrate_legacy_db()
    init_db()
    cleanup_old_call_logs()
    first_run_csv_import()
    init_db()  # Erneut nach möglichem CSV-Import.

    root = tk.Tk()
    app = CallerIDApp(root)

    # Update-Check im Hintergrund; GUI-Dialoge im Hauptthread via after().
    def _update_check_worker():
        result = check_for_update()
        if not result:
            return
        version, url = result
        logger.info("Update verfügbar: %s → Installation wird gestartet.", version)

        def _install_worker():
            if download_and_install_update(url, version):
                root.after(0, lambda: (
                    messagebox.showinfo(
                        "Update",
                        f"Update auf Version {version} wurde installiert.\n"
                        "Bitte starten Sie das Programm neu.",
                    ),
                    os._exit(0),
                ))

        root.after(
            0,
            lambda: messagebox.showinfo(
                "Update",
                f"Eine neue Version ({version}) ist verfügbar.\n"
                "Das Update wird jetzt heruntergeladen und installiert.",
            ),
        )
        threading.Thread(target=_install_worker, daemon=True).start()

    if GITHUB_REPO:
        threading.Thread(target=_update_check_worker, daemon=True).start()

    root.mainloop()
