"""
StillWatching - "Are you still watching?" for desktop video players and
streaming sites in the browser.

Sits in the system tray. When a video is playing (a player is running or a
browser shows YouTube / Netflix / HBO Max ..., and sound is coming out of it)
and nobody has touched the mouse/keyboard for a while, it pauses the video and
asks whether you're still watching. If nobody answers in time it can do
nothing, close the player, put the PC to sleep, hibernate or shut down.

https://github.com/nyorbee
"""
# ---- crash guard first: an error anywhere during startup (even in an import) is shown and
# logged instead of the exe silently disappearing (there is no console window).
import ctypes
import faulthandler
import os
import sys
import traceback

_CRASH_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "StillWatching")


def _startup_crash(exc_type, exc, tb):
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    path = os.path.join(_CRASH_DIR, "crash.log")
    try:
        os.makedirs(_CRASH_DIR, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"--- {__import__('datetime').datetime.now():%Y-%m-%d %H:%M:%S}\n{text}\n")
    except OSError:
        pass
    try:
        ctypes.windll.user32.MessageBoxW(0, f"StillWatching crashed:\n\n{text[-1500:]}\n\nLog: {path}",
                                         "StillWatching", 0x10 | 0x1000)
    except Exception:
        pass


sys.excepthook = _startup_crash
try:  # native crashes (access violation in a DLL) -> Python traceback in crash_native.log
    os.makedirs(_CRASH_DIR, exist_ok=True)
    _native_log = open(os.path.join(_CRASH_DIR, "crash_native.log"), "a", encoding="utf-8")
    faulthandler.enable(_native_log)
except OSError:
    pass



def _boot_mark(stage):
    """Tiny trace of how far startup got (boot.log, last 50 lines kept small by rotation in main)."""
    try:
        with open(os.path.join(_CRASH_DIR, "boot.log"), "a", encoding="utf-8") as f:
            f.write(f"{__import__('time').strftime('%Y-%m-%d %H:%M:%S')} pid {os.getpid()} {stage}\n")
    except OSError:
        pass


_boot_mark(f"start {sys.executable}")

import ctypes.wintypes as wt
import datetime
import json
import logging
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
import winreg
import winsound
from logging.handlers import RotatingFileHandler
from tkinter import messagebox, ttk

try:
    import psutil
    import pystray
    from PIL import Image, ImageDraw, ImageGrab, ImageTk
except ImportError as _missing:  # e.g. exe built with a Python that lacks the packages
    ctypes.windll.user32.MessageBoxW(
        0, f"StillWatching can't start - missing module: {_missing.name}\n\n"
           "If you built the exe yourself, install the packages into the Python used for the build:\n"
           "python -m pip install -r requirements.txt", "StillWatching", 0x10 | 0x1000)
    sys.exit(1)

AUDIO_IMPORT_ERROR = ""
try:
    import comtypes
    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
    AUDIO_OK = True
except Exception as _e:  # pycaw/comtypes missing -> work without the sound check
    AUDIO_OK = False
    AUDIO_IMPORT_ERROR = repr(_e)
_boot_mark(f"imports ok (sound check: {AUDIO_OK})")

APP_NAME = "StillWatching"
VERSION = "1.3.1"
GITHUB_REPO = "nyorbee/still_watchnig"
GITHUB_URL = f"https://github.com/{GITHUB_REPO}"
ITGUY_URL = "https://www.facebook.com/ajtiguy"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.environ.get("APPDATA", BASE_DIR), APP_NAME)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
LOG_PATH = os.path.join(DATA_DIR, "still_watching.log")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

DEFAULT_PLAYERS = [
    "zuikaku.exe", "PotPlayerMini64.exe", "PotPlayerMini.exe", "PotPlayer64.exe", "PotPlayer.exe",
    "vlc.exe", "mpc-hc64.exe", "mpc-hc.exe", "mpc-be64.exe", "mpc-be.exe",
    "mpv.exe", "smplayer.exe", "wmplayer.exe", "Microsoft.Media.Player.exe",
    "Video.UI.exe", "GOM.exe", "KMPlayer.exe", "KMPlayer64.exe", "5kplayer.exe",
    "DivX Player.exe", "PowerDVD.exe", "kodi.exe",
]
# a process also counts as a player if its .exe lives in a folder containing one of these
DEFAULT_PLAYER_PATH_KEYWORDS = ["potplayer", "daum"]
DEFAULT_BROWSERS = ["firefox.exe", "chrome.exe", "msedge.exe", "opera.exe", "brave.exe",
                    "vivaldi.exe", "librewolf.exe", "waterfox.exe", "floorp.exe"]
# matched (case-insensitive) against the browser window title = title of the active tab
DEFAULT_STREAMING_SITES = [
    "YouTube", "Netflix", "HBO Max", "Max |", "Disney+", "Prime Video", "Apple TV",
    "SkyShowtime", "Player.pl", "Polsat Box Go", "CANAL+", "TVP VOD", "CDA", "Twitch",
    "Viaplay", "Crunchyroll", "Vimeo",
]

DEFAULTS = {
    "language": "en",
    "enabled": True,
    "idle_minutes": 30,
    "answer_timeout_minutes": 5,     # 0 = wait forever
    "timeout_action": "sleep",       # none | close_player | sleep | hibernate | shutdown
    "require_audio": True,
    "pause_method": "space",         # space | media  (browsers always use the media key)
    "watch_browsers": True,
    "active_hours_enabled": False,
    "active_from": "22:00",
    "active_until": "06:00",
    "check_updates": True,
    "update_repo": GITHUB_REPO,
    "last_update_check": 0,
    "dev_unlocked": False,           # 5x click on "Created by IT Guy" in About
    "dev_mode": False,
    "players": DEFAULT_PLAYERS,
    "player_path_keywords": DEFAULT_PLAYER_PATH_KEYWORDS,
    "browsers": DEFAULT_BROWSERS,
    "streaming_sites": DEFAULT_STREAMING_SITES,
}
ACTION_KEYS = ["none", "close_player", "sleep", "hibernate", "shutdown"]
POWER_ACTIONS = {"sleep", "hibernate", "shutdown"}

# developer mode timings (seconds)
DEV_IDLE_S = 15
DEV_ANSWER_S = 20
DEV_POLL_S = 2
POLL_S = 10
PEAK_THRESHOLD = 0.0005
DEV_UNLOCK_CLICKS = 5
DEV_UNLOCK_WINDOW_S = 0.7  # max gap between quick clicks
UPDATE_INTERVAL_S = 24 * 3600

# ------------------------------------------------------------------- texts
TEXTS = {
    "en": {
        "enabled": "Checking enabled",
        "ask_after": "Ask after inactivity of",
        "no_answer": "If there's no answer within",
        "never": "Never - wait for me",
        "then": "...then",
        "act_none": "Do nothing (leave it paused)",
        "act_close_player": "Close the player",
        "act_sleep": "Put the computer to sleep",
        "act_hibernate": "Hibernate",
        "act_shutdown": "Shut down the computer",
        "phr_none": "just leave it paused",
        "phr_close_player": "close the player",
        "phr_sleep": "put the computer to sleep",
        "phr_hibernate": "hibernate the computer",
        "phr_shutdown": "shut down the computer",
        "pause_with": "Pause with",
        "space_key": "Space key (in the player window)",
        "media_key": "Media Play/Pause key",
        "only_sound": "Only when the player is making sound",
        "browsers": "Also in browsers (YouTube, Netflix, HBO Max...)",
        "hours": "Active hours",
        "hours_on": "Only within active hours",
        "hours_from": "From",
        "hours_until": "Until",
        "autostart": "Start with Windows",
        "language": "Language / Język",
        "dev": "Developer",
        "dev_mode": f"Developer mode ({DEV_IDLE_S} s inactivity, {DEV_ANSWER_S} s countdown)",
        "dev_diag": "Diagnostics window",
        "dev_force": "Run full check now (ignore inactivity)",
        "dev_ask": "Show the question only",
        "dev_shots": "Make README screenshots (EN + PL)",
        "n_shots": "Saved {n} screenshots to {path}",
        "edit": "Edit settings file (players, sites)",
        "about": "About",
        "open_log": "Open log",
        "quit": "Quit",
        "tip_off": "paused",
        "tip_on": "asks after {t} of inactivity",
        "tip_hours": "active {a}-{b}",
        "q_title": "Are you still watching?",
        "q_paused": "The video is paused.",
        "q_countdown": "If nobody answers in {t}, I will {action}.",
        "q_yes": "Yes, I'm still watching",
        "q_dev": "[DEV] power actions are only simulated",
        "already": f"{APP_NAME} is already running (look for the eye in the tray).",
        "shutdown_msg": "StillWatching: nobody answered. Shutting down in 60 s (cancel: shutdown /a).",
        "n_no_target": "No playing video found.",
        "n_silent": "{name} found, but it's silent - not asking.",
        "n_simulated": "[DEV] would now: {action}",
        "diag_title": "StillWatching - diagnostics",
        "about_dots": "About...",
        "about_title": "About StillWatching",
        "btn_close": "Close",
        "settings": "Settings...",
        "set_title": "StillWatching - settings",
        "tab_general": "General",
        "tab_players": "Players",
        "tab_browsers": "Browsers",
        "tab_sites": "Streaming sites",
        "lbl_idle": "Ask after inactivity (minutes):",
        "lbl_timeout": "Time to answer (minutes, 0 = wait forever):",
        "lbl_from": "Active hours from (HH:MM):",
        "lbl_until": "Active hours until (HH:MM):",
        "lbl_players": "Player .exe file names:",
        "lbl_keywords": "...or the .exe folder path contains:",
        "lbl_browsers": "Browser .exe file names:",
        "lbl_sites": "Text in the tab title (case doesn't matter):",
        "btn_add": "Add",
        "btn_remove": "Remove",
        "btn_pick": "Pick from open windows...",
        "btn_defaults": "Defaults",
        "btn_save": "Save",
        "btn_cancel": "Cancel",
        "pick_title": "Pick a program",
        "pick_hint": "Double-click a program to add it:",
        "err_number": "{field} enter a number from {a} to {b}.",
        "err_time": "Enter the time as HH:MM, e.g. 22:00.",
        "upd_check": "Check for updates",
        "upd_auto": "Check for updates automatically",
        "upd_download": "Download v{v}",
        "n_upd_new": "New version {v} is available (About menu).",
        "n_upd_latest": "You have the latest version ({v}).",
        "n_upd_fail": "Could not check for updates.",
        "n_dev_on": "Developer options unlocked.",
        "n_dev_off": "Developer options hidden.",
        "n_dev_more": "{n} more...",
        "n_pause_fail": "Could not pause {name} - sound keeps playing.",
        "n_action_fail": "Could not {action}.",
    },
    "pl": {
        "enabled": "Sprawdzanie włączone",
        "ask_after": "Pytaj po bezczynności",
        "no_answer": "Jeśli brak odpowiedzi przez",
        "never": "Nigdy - czekaj na mnie",
        "then": "...wtedy",
        "act_none": "Nic nie rób (zostaw pauzę)",
        "act_close_player": "Zamknij odtwarzacz",
        "act_sleep": "Uśpij komputer",
        "act_hibernate": "Hibernuj",
        "act_shutdown": "Wyłącz komputer",
        "phr_none": "zostawię film zapauzowany",
        "phr_close_player": "zamknę odtwarzacz",
        "phr_sleep": "uśpię komputer",
        "phr_hibernate": "zahibernuję komputer",
        "phr_shutdown": "wyłączę komputer",
        "pause_with": "Pauzuj przez",
        "space_key": "Spacja (w oknie odtwarzacza)",
        "media_key": "Klawisz multimedialny Play/Pauza",
        "only_sound": "Tylko gdy z odtwarzacza leci dźwięk",
        "browsers": "Także w przeglądarce (YouTube, Netflix, HBO Max...)",
        "hours": "Godziny działania",
        "hours_on": "Tylko w godzinach działania",
        "hours_from": "Od",
        "hours_until": "Do",
        "autostart": "Uruchamiaj razem z Windows",
        "language": "Language / Język",
        "dev": "Deweloper",
        "dev_mode": f"Tryb deweloperski ({DEV_IDLE_S} s bezczynności, {DEV_ANSWER_S} s odliczania)",
        "dev_diag": "Okno diagnostyki",
        "dev_force": "Pełne sprawdzenie teraz (pomiń bezczynność)",
        "dev_ask": "Pokaż samo pytanie",
        "dev_shots": "Zrób screeny do README (EN + PL)",
        "n_shots": "Zapisano {n} screenów w {path}",
        "edit": "Edytuj plik ustawień (odtwarzacze, strony)",
        "about": "O programie",
        "open_log": "Otwórz log",
        "quit": "Zakończ",
        "tip_off": "wstrzymane",
        "tip_on": "pyta po {t} bezczynności",
        "tip_hours": "działa {a}-{b}",
        "q_title": "Oglądasz jeszcze?",
        "q_paused": "Film jest zapauzowany.",
        "q_countdown": "Jeśli nikt nie odpowie w ciągu {t}, {action}.",
        "q_yes": "Tak, oglądam",
        "q_dev": "[DEV] akcje zasilania są tylko symulowane",
        "already": f"{APP_NAME} już działa (szukaj oka w zasobniku).",
        "shutdown_msg": "StillWatching: brak odpowiedzi. Wyłączenie za 60 s (anuluj: shutdown /a).",
        "n_no_target": "Nie znaleziono odtwarzanego filmu.",
        "n_silent": "Znaleziono {name}, ale nie leci dźwięk - nie pytam.",
        "n_simulated": "[DEV] teraz bym: {action}",
        "diag_title": "StillWatching - diagnostyka",
        "about_dots": "O programie...",
        "about_title": "O programie StillWatching",
        "btn_close": "Zamknij",
        "settings": "Ustawienia...",
        "set_title": "StillWatching - ustawienia",
        "tab_general": "Ogólne",
        "tab_players": "Odtwarzacze",
        "tab_browsers": "Przeglądarki",
        "tab_sites": "Strony z filmami",
        "lbl_idle": "Pytaj po bezczynności (minuty):",
        "lbl_timeout": "Czas na odpowiedź (minuty, 0 = czekaj bez końca):",
        "lbl_from": "Godziny działania od (GG:MM):",
        "lbl_until": "Godziny działania do (GG:MM):",
        "lbl_players": "Nazwy plików .exe odtwarzaczy:",
        "lbl_keywords": "...albo ścieżka folderu z .exe zawiera:",
        "lbl_browsers": "Nazwy plików .exe przeglądarek:",
        "lbl_sites": "Tekst w tytule karty (wielkość liter bez znaczenia):",
        "btn_add": "Dodaj",
        "btn_remove": "Usuń",
        "btn_pick": "Wybierz z otwartych okien...",
        "btn_defaults": "Domyślne",
        "btn_save": "Zapisz",
        "btn_cancel": "Anuluj",
        "pick_title": "Wybierz program",
        "pick_hint": "Kliknij dwukrotnie program, aby go dodać:",
        "err_number": "{field} wpisz liczbę od {a} do {b}.",
        "err_time": "Wpisz godzinę jako GG:MM, np. 22:00.",
        "upd_check": "Sprawdź aktualizacje",
        "upd_auto": "Sprawdzaj aktualizacje automatycznie",
        "upd_download": "Pobierz v{v}",
        "n_upd_new": "Dostępna jest nowa wersja {v} (menu O programie).",
        "n_upd_latest": "Masz najnowszą wersję ({v}).",
        "n_upd_fail": "Nie udało się sprawdzić aktualizacji.",
        "n_dev_on": "Opcje deweloperskie odblokowane.",
        "n_dev_off": "Opcje deweloperskie ukryte.",
        "n_dev_more": "Jeszcze {n}...",
        "n_pause_fail": "Nie udało się zapauzować {name} - dźwięk dalej leci.",
        "n_action_fail": "Nie udało się: {action}.",
    },
}


def pl_plural(n, one, few, many):
    if n == 1:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def duration_label(minutes, lang):
    if minutes >= 60 and minutes % 60 == 0:
        h = minutes // 60
        return f"{h} " + (pl_plural(h, "godzina", "godziny", "godzin") if lang == "pl"
                          else "hour" + ("s" if h > 1 else ""))
    return f"{minutes} " + (pl_plural(minutes, "minuta", "minuty", "minut") if lang == "pl"
                            else "minute" + ("s" if minutes != 1 else ""))


log = logging.getLogger(APP_NAME)

# --------------------------------------------------------------------- Win32
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

kernel32.GetTickCount.restype = wt.DWORD
user32.GetForegroundWindow.restype = wt.HWND
for _fn in ("SetForegroundWindow", "BringWindowToTop", "IsWindowVisible", "IsIconic",
            "GetWindowTextLengthW"):
    getattr(user32, _fn).argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]

user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.MonitorFromWindow.restype = wt.HMONITOR
user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
user32.MonitorFromPoint.restype = wt.HMONITOR
user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.c_void_p]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
kernel32.GetCurrentProcess.restype = wt.HANDLE

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]

VK_SPACE = 0x20
VK_K = 0x4B  # YouTube's own play/pause shortcut (doesn't scroll or press buttons like Space can)
VK_MEDIA_PLAY_PAUSE = 0xB3
KEYEVENTF_EXTENDEDKEY = 0x1
KEYEVENTF_KEYUP = 0x2
WM_CLOSE = 0x0010
CREATE_NO_WINDOW = 0x08000000


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wt.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wt.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]


def enable_shutdown_privilege():
    """SetSuspendState needs SeShutdownPrivilege enabled in our token."""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    token = wt.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0020 | 0x0008, ctypes.byref(token)):
        return False
    try:
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(None, "SeShutdownPrivilege", ctypes.byref(luid)):
            return False
        tp = TOKEN_PRIVILEGES(1, (LUID_AND_ATTRIBUTES * 1)(LUID_AND_ATTRIBUTES(luid, 0x2)))
        advapi32.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None)
        return ctypes.get_last_error() == 0
    finally:
        kernel32.CloseHandle(token)


def _work_area(hmon):
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        r = mi.rcWork
        return r.left, r.top, r.right, r.bottom
    return None


def work_area_of(hwnd):
    """(left, top, right, bottom) of the work area of the monitor showing hwnd."""
    return _work_area(user32.MonitorFromWindow(hwnd, 2))  # MONITOR_DEFAULTTONEAREST


def work_area_at_cursor():
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return _work_area(user32.MonitorFromPoint(pt, 2))


def center_window(top, ref_hwnd=None):
    """Centers a Tk window exactly (real outer size incl. title bar) on the monitor of
    ref_hwnd (e.g. the video player) or, without it, on the monitor with the mouse."""
    top.update_idletasks()
    try:
        hwnd = int(top.wm_frame(), 16)
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        area = (work_area_of(ref_hwnd) if ref_hwnd else None) or work_area_at_cursor()
        if area is None or w <= 1:
            raise OSError("no monitor info")
        left, upper, right, lower = area
        x = left + max(0, (right - left - w) // 2)
        y = upper + max(0, (lower - upper - h) // 2)
        user32.SetWindowPos(hwnd, None, x, y, 0, 0, 0x0001 | 0x0004 | 0x0010)  # NOSIZE|NOZORDER|NOACTIVATE
    except Exception:  # fallback: Tk's own idea of the screen
        w, h = top.winfo_width(), top.winfo_height()
        top.geometry(f"+{(top.winfo_screenwidth() - w) // 2}+{(top.winfo_screenheight() - h) // 2}")


def idle_seconds():
    """Seconds since the last keyboard/mouse input (system-wide)."""
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    return ((kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF) / 1000.0


def window_title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def window_pid(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def windows_of(pids):
    """Visible, titled top-level windows belonging to the given PIDs."""
    found = []

    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            if window_pid(hwnd) in pids:
                found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


def all_windows():
    """(hwnd, pid, title) of all visible, titled top-level windows."""
    found = []

    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            found.append((hwnd, window_pid(hwnd), window_title(hwnd)))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


def version_tuple(text):
    return tuple(int(x) for x in re.findall(r"\d+", text or "")[:4]) or (0,)


def window_area(hwnd):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.right - r.left) * (r.bottom - r.top)


def force_foreground(hwnd):
    """SetForegroundWindow that also works from a background process."""
    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return True
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    me = kernel32.GetCurrentThreadId()
    attached = bool(fg_tid and fg_tid != me and user32.AttachThreadInput(me, fg_tid, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(me, fg_tid, False)
    time.sleep(0.1)
    return user32.GetForegroundWindow() == hwnd  # verify, never type into the wrong window


def tap_key(vk, extended=False):
    flags = KEYEVENTF_EXTENDEDKEY if extended else 0
    user32.keybd_event(vk, 0, flags, 0)
    time.sleep(0.05)
    user32.keybd_event(vk, 0, flags | KEYEVENTF_KEYUP, 0)


def single_instance():
    handle = kernel32.CreateMutexW(None, False, "Local\\nyorbee_StillWatching")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def parse_hhmm(text):
    try:
        h, m = text.strip().split(":")
        return datetime.time(int(h) % 24, int(m) % 60)
    except Exception:
        return None


def within_hours(start_txt, end_txt, now=None):
    start, end = parse_hhmm(start_txt), parse_hhmm(end_txt)
    if start is None or end is None or start == end:
        return True
    t = (now or datetime.datetime.now()).time()
    if start < end:
        return start <= t < end
    return t >= start or t < end  # range over midnight, e.g. 22:00-06:00


# --------------------------------------------------------------- autostart
def launch_command():
    compiled = globals().get("__compiled__")
    if compiled is not None:  # Nuitka (onefile: path of the original .exe)
        exe = getattr(compiled, "original_argv0", None) or sys.argv[0]
        return f'"{os.path.abspath(exe)}"'
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pyw if os.path.exists(pyw) else sys.executable}" "{os.path.abspath(__file__)}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def autostart_value():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, APP_NAME)[0]
    except OSError:
        return None


def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


# ------------------------------------------------------------------ config
class Config:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = dict(DEFAULTS)
        self.mtime = 0.0
        self.load()

    VALID = {
        "language": lambda v: v in TEXTS,
        "idle_minutes": lambda v: 1 <= v <= 1440,
        "answer_timeout_minutes": lambda v: 0 <= v <= 240,
        "timeout_action": lambda v: v in ACTION_KEYS,
        "pause_method": lambda v: v in ("space", "media"),
        "active_from": lambda v: parse_hhmm(v) is not None,
        "active_until": lambda v: parse_hhmm(v) is not None,
    }

    def _valid(self, key, value):
        default = DEFAULTS[key]
        if type(value) is not type(default):
            return False
        if isinstance(default, list):
            return all(isinstance(x, str) and x.strip() for x in value)
        check = self.VALID.get(key)
        return check(value) if check else True

    def load(self):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                stored = json.load(f)
            if not isinstance(stored, dict):
                raise ValueError("settings file is not a JSON object")
            for k, v in stored.items():
                if k in DEFAULTS:
                    if self._valid(k, v):
                        self.data[k] = v
                    else:
                        log.warning("Invalid value for %s in settings: %r - using %r", k, v, self.data[k])
            if self.data["update_repo"].lower() == "nyorbee/stillwatching":  # old wrong default
                self.data["update_repo"] = GITHUB_REPO
                stored.pop("update_repo", None)  # forces a save below
            # keep newly added default players (e.g. after an update) in an older list
            if "zuikaku.exe" not in [p.lower() for p in self.data["players"]]:
                self.data["players"] = ["zuikaku.exe"] + self.data["players"]
            if any(k not in stored for k in DEFAULTS):
                self.save()  # write new options into an old file so they can be edited
            self.mtime = os.path.getmtime(CONFIG_PATH)
        except FileNotFoundError:
            self.save()
        except Exception:
            # broken JSON (e.g. a typo while editing): keep the user's file, don't overwrite it
            log.exception("Could not read %s - keeping previous/default settings", CONFIG_PATH)
            try:
                self.mtime = os.path.getmtime(CONFIG_PATH)
                with open(CONFIG_PATH, encoding="utf-8", errors="replace") as src, \
                        open(CONFIG_PATH + ".bad", "w", encoding="utf-8") as dst:
                    dst.write(src.read())
            except OSError:
                pass

    def reload_if_changed(self):
        try:
            if os.path.getmtime(CONFIG_PATH) != self.mtime:
                with self.lock:
                    self.load()
                log.info("Settings reloaded from file")
                return True
        except OSError:
            pass
        return False

    def save(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_PATH)
        self.mtime = os.path.getmtime(CONFIG_PATH)

    def __getitem__(self, key):
        with self.lock:
            return self.data[key]

    def set(self, key, value):
        with self.lock:
            self.data[key] = value
            self.save()

    def update(self, values):
        with self.lock:
            self.data.update(values)
            self.save()

    def lower_list(self, key):
        with self.lock:
            return [x.lower() for x in self.data[key]]


# ------------------------------------------------------------------ target
class Target:
    """Something that plays video: a player or a browser showing a streaming site."""

    def __init__(self, kind, name, pids, hwnd=None, title="", reason=""):
        self.kind, self.name, self.pids = kind, name, set(pids)
        self.hwnd, self.title, self.reason = hwnd, title, reason

    def __str__(self):
        extra = f' "{self.title}"' if self.title else ""
        return f"{self.name} [{self.kind}, PID {', '.join(map(str, sorted(self.pids)))}]{extra}"


# ----------------------------------------------------------------- the app
class App:
    def __init__(self):
        self.cfg = Config()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.force_check = False
        self.ui_queue = queue.Queue()
        self.root = None
        self.dialog_open = False
        self.diag_window = None
        self._audio_error_logged = False
        self._created_clicks = []
        self.settings_window = None
        self.about_window = None
        self.update_info = None  # (version, url) of a newer release

        self.image_on = self._load_icon()
        self.image_off = self.image_on.convert("LA").convert("RGBA")
        self.icon = pystray.Icon(APP_NAME, self._current_image(), self._tooltip(), self._menu())
        self._apply_log_level()

    # ---- helpers
    def t(self, key, **kw):
        lang = self.cfg["language"]
        text = TEXTS.get(lang, TEXTS["en"]).get(key) or TEXTS["en"][key]
        return text.format(**kw) if kw else text

    @property
    def dev(self):
        return self.cfg["dev_unlocked"] and self.cfg["dev_mode"]

    def idle_limit_s(self):
        return DEV_IDLE_S if self.dev else self.cfg["idle_minutes"] * 60

    def answer_limit_s(self):
        if self.cfg["answer_timeout_minutes"] == 0:
            return 0
        return DEV_ANSWER_S if self.dev else self.cfg["answer_timeout_minutes"] * 60

    def notify(self, text, always=False):
        log.info("Notify: %s", text)
        if self.dev or always:
            try:
                self.icon.notify(text, APP_NAME)
            except Exception:
                pass

    def _apply_log_level(self):
        log.setLevel(logging.DEBUG if self.dev else logging.INFO)

    # ---- tray
    def _load_icon(self):
        try:
            return Image.open(os.path.join(BASE_DIR, "eye.png")).convert("RGBA")
        except Exception:
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.ellipse((4, 16, 60, 48), fill="white", outline="black", width=3)
            d.ellipse((22, 22, 42, 42), fill="black")
            return img

    def _current_image(self):
        return self.image_on if self.cfg["enabled"] else self.image_off

    def _tooltip(self):
        prefix = f"{APP_NAME}{' [DEV]' if self.dev else ''} - "
        if not self.cfg["enabled"]:
            return prefix + self.t("tip_off")
        t = f"{DEV_IDLE_S} s" if self.dev else f"{self.cfg['idle_minutes']} min"
        text = prefix + self.t("tip_on", t=t)
        if self.cfg["active_hours_enabled"]:
            text += ", " + self.t("tip_hours", a=self.cfg["active_from"], b=self.cfg["active_until"])
        return text[:127]

    def refresh_tray(self, rebuild_menu=False):
        self.icon.icon = self._current_image()
        self.icon.title = self._tooltip()
        if rebuild_menu:
            self.icon.menu = self._menu()
        self.icon.update_menu()

    def _radio(self, text, key, value):
        def action(icon, item):
            self.cfg.set(key, value)
            self.refresh_tray()
        return pystray.MenuItem(text, action, checked=lambda item: self.cfg[key] == value, radio=True)

    def _toggle(self, text, key, enabled=True, extra=None):
        def action(icon, item):
            self.cfg.set(key, not self.cfg[key])
            log.info("%s = %s", key, self.cfg[key])
            if extra:
                extra()
            self.refresh_tray()
        return pystray.MenuItem(text, action, checked=lambda item: bool(self.cfg[key]) and enabled,
                                enabled=enabled)

    def _set_language(self, lang):
        self.cfg.set("language", lang)
        self.refresh_tray(rebuild_menu=True)

    def _menu(self):
        Item, Menu, t = pystray.MenuItem, pystray.Menu, self.t
        lang = self.cfg["language"]
        hours_from = ["18:00", "19:00", "20:00", "21:00", "22:00", "23:00", "00:00", "01:00", "02:00"]
        hours_until = ["03:00", "04:00", "05:00", "06:00", "07:00", "08:00", "09:00", "10:00"]
        return Menu(
            self._toggle(t("enabled"), "enabled"),
            Item(t("settings"), lambda: self.ui_queue.put(("settings", None))),
            Menu.SEPARATOR,
            Item(t("ask_after"), Menu(
                *[self._radio(duration_label(m, lang), "idle_minutes", m) for m in (5, 15, 30, 45, 60, 90, 120)])),
            Item(t("no_answer"), Menu(
                *[self._radio(duration_label(m, lang), "answer_timeout_minutes", m) for m in (1, 2, 5, 10, 15)],
                self._radio(t("never"), "answer_timeout_minutes", 0))),
            Item(t("then"), Menu(*[self._radio(t("act_" + k), "timeout_action", k) for k in ACTION_KEYS])),
            Item(t("pause_with"), Menu(
                self._radio(t("space_key"), "pause_method", "space"),
                self._radio(t("media_key"), "pause_method", "media"))),
            self._toggle(t("only_sound"), "require_audio", enabled=AUDIO_OK),
            self._toggle(t("browsers"), "watch_browsers"),
            Item(t("hours"), Menu(
                self._toggle(t("hours_on"), "active_hours_enabled"),
                Menu.SEPARATOR,
                Item(t("hours_from"), Menu(*[self._radio(h, "active_from", h) for h in hours_from])),
                Item(t("hours_until"), Menu(*[self._radio(h, "active_until", h) for h in hours_until])))),
            Menu.SEPARATOR,
            Item(t("autostart"), lambda: self.toggle_autostart(), checked=lambda i: autostart_enabled()),
            Item(t("language"), Menu(
                Item("English", lambda: self._set_language("en"),
                     checked=lambda i: self.cfg["language"] == "en", radio=True),
                Item("Polski", lambda: self._set_language("pl"),
                     checked=lambda i: self.cfg["language"] == "pl", radio=True))),
            Item(t("dev"), visible=lambda i: self.cfg["dev_unlocked"], action=Menu(
                self._toggle(t("dev_mode"), "dev_mode", extra=self._apply_log_level),
                Item(t("dev_diag"), lambda: self.ui_queue.put(("diag", None))),
                Item(t("dev_force"), lambda: self.run_forced_check()),
                Item(t("dev_ask"), lambda: self.ui_queue.put(("ask", None))),
                Item(t("dev_shots"), lambda: self.ui_queue.put(("shots", None))),
                Item(t("edit"), lambda: os.startfile(CONFIG_PATH)),
                Item(t("open_log"), lambda: os.path.exists(LOG_PATH) and os.startfile(LOG_PATH)))),
            Item(lambda i: self.t("upd_download", v=self.update_info[0]) if self.update_info else "-",
                 lambda: self.update_info and webbrowser.open_new_tab(self.update_info[1]),
                 visible=lambda i: bool(self.update_info)),
            Item(t("about_dots"), lambda: self.ui_queue.put(("about", None))),
            Item(t("quit"), lambda: self.quit()),
        )

    def created_by_clicked(self):
        """Easter egg: 5 quick clicks on "Created by IT Guy" in the About window show/hide
        the Developer menu. Returns a status text for the About window ("" = nothing to say)."""
        now = time.monotonic()
        if self._created_clicks and now - self._created_clicks[-1] > DEV_UNLOCK_WINDOW_S:
            self._created_clicks = []
        self._created_clicks.append(now)
        left = DEV_UNLOCK_CLICKS - len(self._created_clicks)
        if left > 0:
            return self.t("n_dev_more", n=left) if len(self._created_clicks) >= 2 else ""
        self._created_clicks = []
        unlocked = not self.cfg["dev_unlocked"]
        self.cfg.set("dev_unlocked", unlocked)
        if not unlocked:
            self.cfg.set("dev_mode", False)
        self._apply_log_level()
        log.info("Developer options %s", "unlocked" if unlocked else "hidden")
        self.refresh_tray(rebuild_menu=True)
        return self.t("n_dev_on" if unlocked else "n_dev_off")

    def check_updates_async(self, manual=False):
        threading.Thread(target=self.check_updates, args=(manual,), daemon=True).start()

    def check_updates(self, manual=False):
        """Looks at the latest GitHub release. Silent unless something is found (or manual)."""
        url = f"https://api.github.com/repos/{self.cfg['update_repo']}/releases/latest"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{VERSION}",
                                                       "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.load(r)
            self.cfg.set("last_update_check", int(time.time()))
            latest = str(data.get("tag_name") or "").lstrip("vV")
            page = data.get("html_url") or f"https://github.com/{self.cfg['update_repo']}/releases"
            if latest and version_tuple(latest) > version_tuple(VERSION):
                log.info("Update available: %s", latest)
                self.update_info = (latest, page)
                self.notify(self.t("n_upd_new", v=latest), always=True)
                self.refresh_tray(rebuild_menu=True)
            elif manual:
                self.notify(self.t("n_upd_latest", v=VERSION), always=True)
        except Exception as e:
            log.info("Update check failed: %r", e)
            if manual:
                self.notify(self.t("n_upd_fail"), always=True)

    def auto_update_loop(self):
        if self.stop.wait(30):  # let Windows finish starting up
            return
        while not self.stop.is_set():
            if (self.cfg["check_updates"]
                    and time.time() - self.cfg["last_update_check"] > UPDATE_INTERVAL_S):
                self.check_updates()
            if self.stop.wait(3600):
                return

    def toggle_autostart(self):
        try:
            set_autostart(not autostart_enabled())
        except OSError:
            log.exception("Could not change autostart")

    def run_forced_check(self):
        self.force_check = True
        self.wake.set()

    def quit(self):
        self.stop.set()
        self.wake.set()
        self.ui_queue.put(("quit", None))
        self.icon.stop()

    # ---- detection
    def scan(self):
        """Returns (targets, processes) - processes is a list of (pid, name, exe) for diagnostics."""
        players = set(self.cfg.lower_list("players"))
        keywords = self.cfg.lower_list("player_path_keywords")
        browsers = set(self.cfg.lower_list("browsers")) if self.cfg["watch_browsers"] else set()
        sites = self.cfg.lower_list("streaming_sites")

        player_procs, browser_pids = {}, {}
        for p in psutil.process_iter(["name", "exe"]):
            name = (p.info.get("name") or "")
            low = name.lower()
            exe = (p.info.get("exe") or "")
            if low in players:
                player_procs[p.pid] = (name, f"name {name}")
            elif keywords and exe and any(k in exe.lower() for k in keywords):
                player_procs[p.pid] = (name, f"path {exe}")
            elif low in browsers:
                browser_pids.setdefault(name, set()).add(p.pid)

        targets = []
        by_name = {}
        for pid, (name, reason) in player_procs.items():
            by_name.setdefault(name, (set(), reason))[0].add(pid)
        for name, (pids, reason) in by_name.items():
            wins = windows_of(pids)
            hwnd = max(wins, key=window_area) if wins else None
            targets.append(Target("player", name, pids, hwnd, window_title(hwnd) if hwnd else "", reason))

        for name, pids in browser_pids.items():
            for hwnd in windows_of(pids):
                title = window_title(hwnd)
                low = title.lower()
                if any(s and s in low for s in sites):
                    targets.append(Target("browser", name, pids, hwnd, title, "site in title"))
                    break
        return targets

    def audio_sessions(self):
        """List of (pid, process name, meter) for all audio sessions."""
        out = []
        for s in AudioUtilities.GetAllSessions():
            try:
                name = s.Process.name() if s.Process else ""
            except Exception:
                name = ""
            out.append((s.ProcessId, name, s._ctl.QueryInterface(IAudioMeterInformation)))
        return out

    def target_meters(self, target):
        names = {target.name.lower()}
        pids = set(target.pids)
        for pid in list(pids):  # audio is often rendered by a child process (browsers)
            try:
                pids.update(c.pid for c in psutil.Process(pid).children(recursive=True))
            except psutil.Error:
                pass
        return [m for pid, name, m in self.audio_sessions()
                if pid in pids or (target.kind == "browser" and name.lower() in names)]

    def wait_sound(self, target, want_sound, seconds, settle=1.5):
        """want_sound=True: True as soon as there is sound (copes with quiet scenes).
        want_sound=False: True once it has been silent for `settle` seconds."""
        meters = self.target_meters(target)
        log.debug("%s: %d audio session(s)", target, len(meters))
        if not meters:
            return not want_sound
        end = time.monotonic() + seconds
        silent_since = None
        while time.monotonic() < end and not self.stop.is_set():
            peak = max(m.GetPeakValue() for m in meters)
            now = time.monotonic()
            if peak > PEAK_THRESHOLD:
                if want_sound:
                    log.debug("%s: sound peak %.4f", target, peak)
                    return True
                silent_since = None
            elif not want_sound:
                silent_since = silent_since or now
                if now - silent_since >= settle:
                    return True
            time.sleep(0.2)
        return False

    def safe_wait_sound(self, target, want_sound, seconds, on_error):
        try:
            return self.wait_sound(target, want_sound, seconds)
        except Exception:
            if not self._audio_error_logged:
                log.exception("Sound check failed")
                self._audio_error_logged = True
            return on_error

    def is_audible(self, target, seconds=5.0):
        return self.safe_wait_sound(target, True, seconds, on_error=True)

    def find_playing(self, check_audio):
        """First target that is playing, or (None, reason)."""
        targets = self.scan()
        if not targets:
            return None, self.t("n_no_target")
        if not check_audio:
            return targets[0], ""
        silent = []
        for tg in targets:
            if self.is_audible(tg):
                return tg, ""
            silent.append(tg.name)
        return None, self.t("n_silent", name=", ".join(silent))

    # ---- player control
    def toggle_playback(self, target, method=None):
        """Sends Play/Pause. Returns the method actually used: "space", "media" or "page_key"."""
        method = method or ("media" if target.kind == "browser" else self.cfg["pause_method"])
        if method == "page_key":  # browser fallback: focus the window, press the site's own shortcut
            hwnd = target.hwnd
            if hwnd and not user32.IsIconic(hwnd) and force_foreground(hwnd):
                youtube = "youtube" in target.title.lower()
                log.debug("%s -> %s", "K" if youtube else "Space", target)
                tap_key(VK_K if youtube else VK_SPACE)
                return "page_key"
            log.info("Browser window can't be focused - using the media key instead")
        elif method == "space":
            hwnd = target.hwnd
            if hwnd and not user32.IsIconic(hwnd) and force_foreground(hwnd):
                log.debug("Space -> %s", target)
                tap_key(VK_SPACE)
                return "space"
            log.info("Player window can't be focused - using the media key instead")
        log.debug("Media key -> %s", target)
        tap_key(VK_MEDIA_PLAY_PAUSE, extended=True)
        return "media"

    def pause(self, target, verify):
        """Pauses and (with the sound check) verifies it. Returns the method or None if it didn't pause."""
        method = self.toggle_playback(target)
        if not verify or self.safe_wait_sound(target, False, 3.0, on_error=True):
            return method
        if method == "space":  # the key may not have reached the player - try the media key once
            log.info("Still playing after Space - trying the media key")
            tap_key(VK_MEDIA_PLAY_PAUSE, extended=True)
            if self.safe_wait_sound(target, False, 3.0, on_error=True):
                return "media"
        elif target.kind == "browser":
            # The media key goes to whichever app Windows thinks is "the" media player - sometimes
            # another one (Spotify, PotPlayer...) or nothing. Fall back to the page's own shortcut.
            log.info("Still playing after the media key - trying the page shortcut in the browser")
            if self.toggle_playback(target, "page_key") == "page_key" and \
                    self.safe_wait_sound(target, False, 3.0, on_error=True):
                return "page_key"
        log.warning("Could not pause %s", target)
        self.notify(self.t("n_pause_fail", name=target.name), always=True)
        return None

    def resume(self, target, method, verify):
        if method is None:
            return  # we didn't pause it, so don't toggle (that could pause it instead)
        if not any(psutil.pid_exists(p) for p in target.pids):
            log.info("Player closed meanwhile - nothing to resume")
            return
        if verify and self.safe_wait_sound(target, True, 0.8, on_error=False):
            log.info("Already playing again (resumed by the user) - not touching it")
            return
        self.toggle_playback(target, method)
        if verify and not self.safe_wait_sound(target, True, 4.0, on_error=True):
            log.info("No sound after resuming %s (quiet scene or didn't resume)", target)

    def timeout_action(self, target):
        action = self.cfg["timeout_action"]
        log.info("No answer - action: %s", action)
        if self.dev and action in POWER_ACTIONS:
            self.notify(self.t("n_simulated", action=self.t("act_" + action)))
            return
        if not self._do_action(action, target):
            self.notify(self.t("n_action_fail", action=self.t("act_" + action).lower()), always=True)

    def _do_action(self, action, target):
        try:
            if action == "close_player":
                if target.kind == "browser":
                    log.info("Not closing a browser - video stays paused")
                    return True
                for hwnd in windows_of(target.pids):  # graceful: the player saves its position
                    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                return True
            elif action in ("sleep", "hibernate"):
                enable_shutdown_privilege()
                powrprof = ctypes.WinDLL("powrprof")
                if powrprof.SetSuspendState(action == "hibernate", False, False):
                    return True
                log.error("SetSuspendState failed (error %d)", ctypes.get_last_error())
                if action == "hibernate":  # e.g. hibernation disabled -> try the built-in command
                    r = subprocess.run(["shutdown", "/h"], creationflags=CREATE_NO_WINDOW, check=False)
                    return r.returncode == 0
                return False
            elif action == "shutdown":
                r = subprocess.run(["shutdown", "/s", "/t", "60", "/c", self.t("shutdown_msg")],
                                   creationflags=CREATE_NO_WINDOW, check=False)
                if r.returncode != 0:
                    log.error("shutdown.exe returned %d", r.returncode)
                    return False
            return True
        except Exception:
            log.exception("Timeout action failed")
            return False

    # ---- dialog bridge (monitor thread -> Tk thread)
    def ask(self, hwnd=None):
        done = threading.Event()
        result = {"answered": False}
        self.ui_queue.put(("ask", (done, result, hwnd)))
        while not done.wait(0.5):
            if self.stop.is_set():
                return True
        return result["answered"]

    def wait_for_real_input(self, since):
        """After injecting keys, wait until a *human* touches the mouse/keyboard."""
        while not self.stop.is_set():
            if idle_seconds() < (time.monotonic() - since) - 1.5:
                return
            self.stop.wait(1 if self.dev else 2)

    # ---- monitor thread
    def monitor(self):
        global AUDIO_OK
        if AUDIO_OK:
            try:
                comtypes.CoInitialize()
            except Exception:
                log.exception("COM init failed - sound check disabled")
                AUDIO_OK = False
        log.info("Monitor started (sound check %s)",
                 "available" if AUDIO_OK else f"NOT available {AUDIO_IMPORT_ERROR}")
        while not self.stop.is_set():
            self.wake.wait(DEV_POLL_S if self.dev else POLL_S)
            self.wake.clear()
            if self.stop.is_set():
                break
            forced, self.force_check = self.force_check, False
            try:
                if self.cfg.reload_if_changed():
                    self._apply_log_level()
                    self.refresh_tray(rebuild_menu=True)
                if self.dialog_open:
                    continue
                if not forced:
                    if not self.cfg["enabled"]:
                        continue
                    if (self.cfg["active_hours_enabled"] and not self.dev
                            and not within_hours(self.cfg["active_from"], self.cfg["active_until"])):
                        continue
                    if idle_seconds() < self.idle_limit_s():
                        continue

                target, why = self.find_playing(AUDIO_OK and self.cfg["require_audio"])
                if target is None:
                    if forced:
                        self.notify(why)
                    else:
                        log.debug("Idle, but: %s", why)
                    continue

                log.info("%s idle %.0f s, playing: %s - asking",
                         "FORCED," if forced else "", idle_seconds(), target)
                verify = AUDIO_OK and self.cfg["require_audio"]
                method = self.pause(target, verify)
                if self.ask(target.hwnd):
                    log.info("Answered - resuming")
                    self.resume(target, method, verify)
                else:
                    injected_at = time.monotonic()
                    self.timeout_action(target)
                    self.wait_for_real_input(injected_at)
                    log.info("User is back")
            except Exception:
                log.exception("Error in monitor loop")

    # ---- Tk side
    def poll_queue(self):
        try:
            while True:
                cmd, arg = self.ui_queue.get_nowait()
                if cmd == "quit":
                    self.root.destroy()
                    return
                if cmd == "shots":
                    if not self.dialog_open:
                        ScreenshotMaker(self).start()
                elif cmd == "about":
                    if self.about_window and self.about_window.alive():
                        self.about_window.top.lift()
                        self.about_window.top.focus_force()
                    else:
                        self.about_window = AboutWindow(self)
                elif cmd == "settings":
                    if self.settings_window and self.settings_window.alive():
                        self.settings_window.top.lift()
                        self.settings_window.top.focus_force()
                    else:
                        self.settings_window = SettingsWindow(self)
                elif cmd == "diag":
                    if self.diag_window and self.diag_window.alive():
                        self.diag_window.top.lift()
                    else:
                        self.diag_window = DiagWindow(self)
                elif cmd == "ask":
                    if self.dialog_open:
                        if arg:
                            arg[1]["answered"] = True
                            arg[0].set()
                        continue
                    try:
                        AskDialog(self, arg)
                    except Exception:
                        # never leave the monitor waiting forever, and never run a power action on a UI bug
                        log.exception("Could not show the question")
                        self.dialog_open = False
                        if arg:
                            arg[1]["answered"] = True
                            arg[0].set()
        except queue.Empty:
            pass
        self.root.after(200, self.poll_queue)

    def run(self):
        cmd = launch_command()
        if autostart_value() not in (None, cmd):  # exe moved/renamed -> fix the autostart entry
            try:
                set_autostart(True)
                log.info("Autostart path updated to %s", cmd)
            except OSError:
                log.exception("Could not update autostart")
        self.root = tk.Tk()
        self.root.withdraw()
        threading.Thread(target=self.icon.run, daemon=True).start()
        threading.Thread(target=self.monitor, daemon=True).start()
        threading.Thread(target=self.auto_update_loop, daemon=True).start()
        self.root.after(200, self.poll_queue)
        self.root.mainloop()
        self.stop.set()
        self.wake.set()
        self.icon.stop()


def created_footer(parent, img):
    """Black "Created by IT Guy" strip. Left half -> Facebook, right half -> GitHub."""
    footer = tk.Label(parent, bg="black", cursor="hand2", pady=4)
    if img:
        footer.config(image=img)
    else:
        footer.config(text="Created by IT Guy", fg="white", font=("Consolas", 10))
    footer.pack(fill="x")
    return footer


def created_half_url(event):
    return ITGUY_URL if event.x < event.widget.winfo_width() / 2 else GITHUB_URL


class AskDialog:
    def __init__(self, app, waiter):
        self.app, self.waiter = app, waiter
        target_hwnd = waiter[2] if waiter else None
        self.limit = app.answer_limit_s()
        self.remaining = self.limit
        self.action_text = app.t("phr_" + app.cfg["timeout_action"])
        app.dialog_open = True

        top = self.top = tk.Toplevel(app.root)
        top.attributes("-alpha", 0.0)  # invisible until it is centered - no jumping around
        top.title(APP_NAME)
        top.resizable(False, False)
        top.attributes("-topmost", True)
        top.protocol("WM_DELETE_WINDOW", lambda: self.finish(True))
        try:
            top.iconbitmap(os.path.join(BASE_DIR, "eye.ico"))
        except tk.TclError:
            pass

        frame = tk.Frame(top, padx=28, pady=20)
        frame.pack()
        tk.Label(frame, text=app.t("q_title"), font=("Segoe UI", 18, "bold")).pack()
        self.info = tk.Label(frame, font=("Segoe UI", 10), fg="#555", justify="center")
        self.info.pack(pady=(8, 14))
        btn = tk.Button(frame, text=app.t("q_yes"), font=("Segoe UI", 11),
                        padx=16, pady=6, command=lambda: self.finish(True))
        btn.pack()
        if app.dev:
            tk.Label(frame, text=app.t("q_dev"), font=("Segoe UI", 8), fg="#b00").pack(pady=(10, 0))

        self.created_img = self._created_by_image(round(22 * top.winfo_fpixels("1i") / 96))
        footer = created_footer(top, self.created_img)
        footer.bind("<Button-1>", lambda e: webbrowser.open_new_tab(created_half_url(e)))

        top.bind("<Return>", lambda e: self.finish(True))
        top.bind("<Escape>", lambda e: self.finish(True))

        self.target_hwnd = target_hwnd
        self.info.config(text=self._info_text())  # final size before centering
        center_window(top, target_hwnd)  # same monitor as the video
        top.attributes("-alpha", 1.0)
        try:
            force_foreground(int(top.wm_frame(), 16))
        except Exception:
            pass
        top.focus_force()
        btn.focus_set()
        winsound.MessageBeep(winsound.MB_ICONQUESTION)
        self.tick()

    @staticmethod
    def _created_by_image(h):
        try:
            img = Image.open(os.path.join(BASE_DIR, "created.png")).convert("RGB")
            img = img.resize((round(img.width * h / img.height), h), Image.LANCZOS)
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    def _info_text(self):
        text = self.app.t("q_paused")
        if self.limit:
            m, s = divmod(int(max(self.remaining, 0)), 60)
            text += "\n" + self.app.t("q_countdown", t=f"{m}:{s:02d}", action=self.action_text)
        return text

    def tick(self):
        if not self.top.winfo_exists():
            return
        if self.limit and self.remaining <= 0:
            self.finish(False)
            return
        self.info.config(text=self._info_text())
        if self.limit:
            self.remaining -= 1
        self.top.attributes("-topmost", True)  # some fullscreen players fight for the top
        self.top.after(1000, self.tick)

    def finish(self, answered):
        if not self.top.winfo_exists():
            return
        self.top.destroy()
        self.app.dialog_open = False
        if self.waiter:
            done, result = self.waiter[0], self.waiter[1]
            result["answered"] = answered
            done.set()


def window_bounds(hwnd):
    """Visible bounds of a window (without the invisible Win10/11 resize border)."""
    r = wt.RECT()
    try:
        dwm = ctypes.WinDLL("dwmapi")
        if dwm.DwmGetWindowAttribute(wt.HWND(hwnd), 9, ctypes.byref(r), ctypes.sizeof(r)) == 0:
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def project_dir():
    """Folder of the .exe (compiled) or of the script - not the onefile temp folder."""
    compiled = globals().get("__compiled__")
    if compiled is not None:
        return os.path.dirname(os.path.abspath(getattr(compiled, "original_argv0", None) or sys.argv[0]))
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


class ScreenshotMaker:
    """Opens each window in English and Polish, grabs it and saves PNGs for the README."""

    def __init__(self, app):
        self.app = app
        self.out = os.path.join(project_dir(), "docs", "screenshots")
        self.saved = 0
        self.steps = [(lang, kind) for lang in ("en", "pl")
                      for kind in ("ask", "about", "settings_general", "settings_players")]
        with app.cfg.lock:  # temporary, not saved: language + no [DEV] note in the dialog
            self.orig = (app.cfg.data["language"], app.cfg.data["dev_mode"])
            app.cfg.data["dev_mode"] = False

    def start(self):
        os.makedirs(self.out, exist_ok=True)
        self.app.dialog_open = True  # keep the monitor quiet meanwhile
        self.next()

    def next(self):
        if not self.steps:
            return self.done()
        lang, kind = self.steps.pop(0)
        self.app.dialog_open = True
        with self.app.cfg.lock:
            self.app.cfg.data["language"] = lang
        try:
            if kind == "ask":
                win = AskDialog(self.app, None)
                self.app.dialog_open = True
                top, close = win.top, lambda: win.finish(True)
            elif kind == "about":
                win = AboutWindow(self.app)
                top, close = win.top, win.top.destroy
            else:
                win = SettingsWindow(self.app)
                if kind == "settings_players":
                    win.nb.select(1)
                top, close = win.top, win.top.destroy
            top.attributes("-topmost", True)
            top.after(900, lambda: self.grab(top, f"{kind}_{lang}.png", close))
        except Exception:
            log.exception("Screenshot %s/%s failed", kind, lang)
            self.app.root.after(100, self.next)

    def grab(self, top, name, close):
        try:
            top.update()
            box = window_bounds(int(top.wm_frame(), 16))
            ImageGrab.grab(bbox=box, all_screens=True).save(os.path.join(self.out, name))
            self.saved += 1
        except Exception:
            log.exception("Could not grab %s", name)
        close()
        self.app.root.after(300, self.next)

    def done(self):
        with self.app.cfg.lock:
            self.app.cfg.data["language"], self.app.cfg.data["dev_mode"] = self.orig
        self.app.dialog_open = False
        self.app.notify(self.app.t("n_shots", n=self.saved, path=self.out), always=True)
        try:
            os.startfile(self.out)
        except OSError:
            pass


class AboutWindow:
    def __init__(self, app):
        self.app = app
        t = app.t
        top = self.top = tk.Toplevel(app.root)
        top.attributes("-alpha", 0.0)
        top.title(t("about_title"))
        top.resizable(False, False)
        try:
            top.iconbitmap(os.path.join(BASE_DIR, "eye.ico"))
        except tk.TclError:
            pass
        scale = top.winfo_fpixels("1i") / 96
        frame = tk.Frame(top, padx=30, pady=18)
        frame.pack()
        self.logo = None
        try:
            size = round(72 * scale)
            img = app.image_on.copy()
            img.thumbnail((size, size), Image.LANCZOS)
            self.logo = ImageTk.PhotoImage(img)
            tk.Label(frame, image=self.logo).pack()
        except Exception:
            pass
        tk.Label(frame, text=APP_NAME, font=("Segoe UI", 16, "bold")).pack(pady=(6, 0))
        tk.Label(frame, text=f"v{VERSION}", font=("Segoe UI", 10), fg="#555").pack()

        links = tk.Frame(frame)
        links.pack(pady=(10, 4))
        for text, url in (("GitHub", GITHUB_URL), ("IT Guy Facebook", ITGUY_URL)):
            lbl = tk.Label(links, text=text, fg="#0563c1", cursor="hand2", font=("Segoe UI", 10, "underline"))
            lbl.pack(side="left", padx=8)
            lbl.bind("<Button-1>", lambda e, u=url: webbrowser.open_new_tab(u))

        upd = tk.Frame(frame)
        upd.pack(pady=(6, 0))
        self.auto_var = tk.BooleanVar(value=app.cfg["check_updates"])
        ttk.Checkbutton(upd, text=t("upd_auto"), variable=self.auto_var,
                        command=lambda: app.cfg.set("check_updates", self.auto_var.get())).pack(anchor="w")
        ttk.Button(upd, text=t("upd_check"),
                   command=lambda: app.check_updates_async(manual=True)).pack(pady=(4, 0))
        if app.update_info:
            new = tk.Label(frame, text=t("upd_download", v=app.update_info[0]), fg="#0563c1",
                           cursor="hand2", font=("Segoe UI", 10, "bold underline"))
            new.pack(pady=(6, 0))
            new.bind("<Button-1>", lambda e: webbrowser.open_new_tab(app.update_info[1]))

        self.status = tk.Label(frame, text="", font=("Segoe UI", 9), fg="#b00")
        self.status.pack(pady=(8, 0))
        ttk.Button(frame, text=t("btn_close"), command=top.destroy).pack(pady=(4, 0))

        # "Created by IT Guy" strip - 5 quick clicks = developer menu on/off
        self.created_img = AskDialog._created_by_image(round(22 * scale))
        footer = created_footer(top, self.created_img)
        footer.bind("<Button-1>", self.on_created)

        top.bind("<Escape>", lambda e: top.destroy())
        center_window(top)
        top.attributes("-alpha", 1.0)
        top.focus_force()
        self._clear_job = None
        self._link_job = None

    def alive(self):
        try:
            return bool(self.top.winfo_exists())
        except tk.TclError:
            return False

    def on_created(self, event):
        """One click opens the link (Facebook / GitHub half). Quick clicks in a row don't open
        anything - 5 of them show/hide the developer menu."""
        url = created_half_url(event)
        if self._link_job:
            self.top.after_cancel(self._link_job)
            self._link_job = None
        msg = self.app.created_by_clicked()
        if len(self.app._created_clicks) == 1:  # first click of a burst: open unless more follow
            delay = int(DEV_UNLOCK_WINDOW_S * 1000)
            self._link_job = self.top.after(delay, lambda: webbrowser.open_new_tab(url))
        self.status.config(text=msg)
        if self._clear_job:
            self.top.after_cancel(self._clear_job)
        self._clear_job = self.top.after(3000 if msg else 10, lambda: self.status.config(text=""))


class ListEditor:
    """Listbox + entry with Add / Remove / Defaults."""

    def __init__(self, parent, app, label, items, defaults, pick=None):
        self.app, self.defaults = app, defaults
        t = app.t
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, pady=(0, 8))
        ttk.Label(frame, text=label).pack(anchor="w")
        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True)
        self.lb = tk.Listbox(body, height=7, activestyle="none", exportselection=False, selectmode="extended")
        sb = ttk.Scrollbar(body, orient="vertical", command=self.lb.yview)
        self.lb.config(yscrollcommand=sb.set)
        self.lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        side = ttk.Frame(body)
        side.pack(side="left", fill="y", padx=(6, 0))
        ttk.Button(side, text=t("btn_remove"), command=self.remove).pack(fill="x")
        ttk.Button(side, text=t("btn_defaults"), command=lambda: self.set_items(self.defaults)).pack(fill="x", pady=4)
        if pick:
            ttk.Button(side, text=t("btn_pick"), command=pick).pack(fill="x")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(4, 0))
        self.entry = ttk.Entry(row)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: self.add())
        ttk.Button(row, text=t("btn_add"), command=self.add).pack(side="left", padx=(6, 0))
        self.lb.bind("<Delete>", lambda e: self.remove())
        self.set_items(items)

    def set_items(self, items):
        self.lb.delete(0, "end")
        for x in items:
            self.lb.insert("end", x)

    def items(self):
        seen, out = set(), []
        for x in self.lb.get(0, "end"):
            x = x.strip()
            if x and x.lower() not in seen:
                seen.add(x.lower())
                out.append(x)
        return out

    def add(self, value=None):
        value = (value if value is not None else self.entry.get()).strip()
        if value and value.lower() not in (x.lower() for x in self.lb.get(0, "end")):
            self.lb.insert("end", value)
            self.lb.see("end")
        self.entry.delete(0, "end")

    def remove(self):
        for i in reversed(self.lb.curselection()):
            self.lb.delete(i)


class SettingsWindow:
    def __init__(self, app):
        self.app = app
        cfg, t = app.cfg, app.t
        top = self.top = tk.Toplevel(app.root)
        top.title(t("set_title"))
        top.minsize(540, 480)
        try:
            top.iconbitmap(os.path.join(BASE_DIR, "eye.ico"))
        except tk.TclError:
            pass
        nb = self.nb = ttk.Notebook(top)
        nb.pack(fill="both", expand=True, padx=10, pady=(10, 0))

        gen = ttk.Frame(nb, padding=12)
        nb.add(gen, text=t("tab_general"))
        self.vars = {}
        for row, (key, label) in enumerate([("idle_minutes", "lbl_idle"),
                                            ("answer_timeout_minutes", "lbl_timeout"),
                                            ("active_from", "lbl_from"),
                                            ("active_until", "lbl_until")]):
            ttk.Label(gen, text=t(label)).grid(row=row, column=0, sticky="w", pady=4)
            var = tk.StringVar(value=str(cfg[key]))
            ttk.Entry(gen, textvariable=var, width=8).grid(row=row, column=1, sticky="w", padx=8)
            self.vars[key] = var

        pl = ttk.Frame(nb, padding=12)
        nb.add(pl, text=t("tab_players"))
        self.players = ListEditor(pl, app, t("lbl_players"), cfg["players"], DEFAULT_PLAYERS,
                                  pick=self.pick_program)
        self.keywords = ListEditor(pl, app, t("lbl_keywords"), cfg["player_path_keywords"],
                                   DEFAULT_PLAYER_PATH_KEYWORDS)

        br = ttk.Frame(nb, padding=12)
        nb.add(br, text=t("tab_browsers"))
        self.browsers = ListEditor(br, app, t("lbl_browsers"), cfg["browsers"], DEFAULT_BROWSERS)

        st = ttk.Frame(nb, padding=12)
        nb.add(st, text=t("tab_sites"))
        self.sites = ListEditor(st, app, t("lbl_sites"), cfg["streaming_sites"], DEFAULT_STREAMING_SITES)

        bar = ttk.Frame(top, padding=10)
        bar.pack(fill="x")
        ttk.Button(bar, text=t("btn_cancel"), command=top.destroy).pack(side="right")
        ttk.Button(bar, text=t("btn_save"), command=self.save).pack(side="right", padx=6)
        top.bind("<Escape>", lambda e: top.destroy())
        center_window(top)
        top.focus_force()

    def alive(self):
        try:
            return bool(self.top.winfo_exists())
        except tk.TclError:
            return False

    def _number(self, key, label, lo, hi):
        try:
            v = int(self.vars[key].get().strip())
            if lo <= v <= hi:
                return v
        except ValueError:
            pass
        messagebox.showerror(APP_NAME, self.app.t("err_number", field=self.app.t(label), a=lo, b=hi),
                             parent=self.top)
        return None

    def save(self):
        idle = self._number("idle_minutes", "lbl_idle", 1, 1440)
        if idle is None:
            return
        answer = self._number("answer_timeout_minutes", "lbl_timeout", 0, 240)
        if answer is None:
            return
        hours = {}
        for key in ("active_from", "active_until"):
            parsed = parse_hhmm(self.vars[key].get())
            if parsed is None:
                messagebox.showerror(APP_NAME, self.app.t("err_time"), parent=self.top)
                return
            hours[key] = parsed.strftime("%H:%M")
        values = {"idle_minutes": idle, "answer_timeout_minutes": answer, **hours,
                  "players": self.players.items(), "player_path_keywords": self.keywords.items(),
                  "browsers": self.browsers.items(), "streaming_sites": self.sites.items()}
        self.app.cfg.update(values)
        log.info("Settings saved from the settings window")
        self.app.refresh_tray(rebuild_menu=True)
        self.top.destroy()

    def pick_program(self):
        """Lists programs that have a window open now - handy for adding a player."""
        me = os.getpid()
        skip = set(self.app.cfg.lower_list("browsers")) | {
            "explorer.exe", "applicationframehost.exe", "textinputhost.exe", "systemsettings.exe",
            "shellexperiencehost.exe", "searchhost.exe", "startmenuexperiencehost.exe"}
        programs = {}
        for _, pid, title in all_windows():
            if pid == me:
                continue
            try:
                name = psutil.Process(pid).name()
            except psutil.Error:
                continue
            if name.lower() not in skip:
                programs.setdefault(name, title)

        win = tk.Toplevel(self.top)
        win.title(self.app.t("pick_title"))
        win.transient(self.top)
        win.geometry("540x340")
        center_window(win)
        ttk.Label(win, text=self.app.t("pick_hint"), padding=(10, 8)).pack(anchor="w")
        lb = tk.Listbox(win, activestyle="none")
        lb.pack(fill="both", expand=True, padx=10)
        names = sorted(programs, key=str.lower)
        for name in names:
            lb.insert("end", f"{name}   —   {programs[name][:60]}")

        def choose(event=None):
            sel = lb.curselection()
            if sel:
                self.players.add(names[sel[0]])
                win.destroy()

        lb.bind("<Double-Button-1>", choose)
        lb.bind("<Return>", choose)
        bar = ttk.Frame(win, padding=10)
        bar.pack(fill="x")
        ttk.Button(bar, text=self.app.t("btn_cancel"), command=win.destroy).pack(side="right")
        ttk.Button(bar, text=self.app.t("btn_add"), command=choose).pack(side="right", padx=6)


class DiagWindow:
    """Live view of what StillWatching sees - for finding out why a player isn't detected."""

    def __init__(self, app):
        self.app = app
        top = self.top = tk.Toplevel(app.root)
        top.title(app.t("diag_title"))
        top.geometry("820x520")
        try:
            top.iconbitmap(os.path.join(BASE_DIR, "eye.ico"))
        except tk.TclError:
            pass
        self.text = tk.Text(top, font=("Consolas", 9), wrap="none")
        self.text.pack(fill="both", expand=True)
        bar = tk.Frame(top)
        bar.pack(fill="x")
        tk.Button(bar, text="Copy", command=self.copy).pack(side="left", padx=4, pady=4)
        self.paused = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="Freeze", variable=self.paused).pack(side="left")
        self.refresh()
        center_window(top)

    def alive(self):
        try:
            return bool(self.top.winfo_exists())
        except tk.TclError:
            return False

    def copy(self):
        self.top.clipboard_clear()
        self.top.clipboard_append(self.text.get("1.0", "end"))

    def report(self):
        app, cfg = self.app, self.app.cfg
        lines = [
            f"{APP_NAME} v{VERSION}   {datetime.datetime.now():%H:%M:%S}   dev_mode={cfg['dev_mode']}",
            f"Checking enabled: {cfg['enabled']}   sound check available: {AUDIO_OK}"
            f"   require_audio: {cfg['require_audio']}   browsers: {cfg['watch_browsers']}",
            f"Idle: {idle_seconds():.0f} s   (asks after {app.idle_limit_s()} s)",
        ]
        if cfg["active_hours_enabled"]:
            inside = within_hours(cfg["active_from"], cfg["active_until"])
            lines.append(f"Active hours {cfg['active_from']}-{cfg['active_until']}: "
                         f"{'inside' if inside else 'OUTSIDE'}{' (ignored in dev mode)' if app.dev else ''}")
        fg = user32.GetForegroundWindow()
        if fg:
            pid = window_pid(fg)
            try:
                pname = psutil.Process(pid).name()
            except psutil.Error:
                pname = "?"
            lines.append(f"Foreground: {pname} (PID {pid}) \"{window_title(fg)}\"")

        lines += ["", "== Detected video targets =="]
        targets = app.scan()
        if not targets:
            lines.append("  (none) - is your player's .exe in the 'players' list? See all audio sources below.")
        audible_pids = set()
        for tg in targets:
            lines.append(f"  {tg}   <- {tg.reason}")
            if AUDIO_OK:
                try:
                    meters = app.target_meters(tg)
                    peak = max((m.GetPeakValue() for m in meters), default=0.0)
                    lines.append(f"      audio sessions: {len(meters)}   current peak: {peak:.4f}"
                                 f"{'  <- SOUND' if peak > PEAK_THRESHOLD else ''}")
                except Exception as e:
                    lines.append(f"      audio error: {e!r}")
            audible_pids |= tg.pids

        if AUDIO_OK:
            lines += ["", "== All audio sessions (who is making sound) =="]
            try:
                for pid, name, meter in app.audio_sessions():
                    peak = meter.GetPeakValue()
                    mark = "*" if pid in audible_pids else " "
                    lines.append(f" {mark} {name or '(system sounds)':<32} PID {pid:<7} peak {peak:.4f}"
                                 f"{'  <- SOUND' if peak > PEAK_THRESHOLD else ''}")
            except Exception as e:
                lines.append(f"  error: {e!r}")
        lines += ["", f"Settings file: {CONFIG_PATH}", f"Log: {LOG_PATH}"]
        return "\n".join(lines)

    def refresh(self):
        if not self.alive():
            return
        if not self.paused.get():
            try:
                content = self.report()
            except Exception as e:
                content = f"Error: {e!r}"
            self.text.delete("1.0", "end")
            self.text.insert("1.0", content)
        self.top.after(1000, self.refresh)


def setup_logging():
    os.makedirs(DATA_DIR, exist_ok=True)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def main():
    _boot_mark("main")
    try:  # keep boot.log small
        bp = os.path.join(_CRASH_DIR, "boot.log")
        if os.path.getsize(bp) > 20_000:
            os.replace(bp, bp + ".old")
    except OSError:
        pass
    mutex = single_instance()
    if mutex is None:
        _boot_mark("already running - exit")
        cfg_lang = "en"
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg_lang = json.load(f).get("language", "en")
        except Exception:
            pass
        user32.MessageBoxW(0, TEXTS.get(cfg_lang, TEXTS["en"])["already"], APP_NAME, 0x40 | 0x1000)
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp dialog on scaled displays
    except Exception:
        pass
    setup_logging()
    log.info("%s v%s started", APP_NAME, VERSION)
    App().run()
    log.info("Exited")


def fatal(exc_text):
    """Last resort: write crash.log and show it, instead of silently disappearing."""
    path = os.path.join(DATA_DIR, "crash.log")
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"--- {datetime.datetime.now():%Y-%m-%d %H:%M:%S} v{VERSION}\n{exc_text}\n")
    except OSError:
        pass
    user32.MessageBoxW(0, f"StillWatching crashed:\n\n{exc_text[-1500:]}\n\nLog: {path}",
                       APP_NAME, 0x10 | 0x1000)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        fatal(traceback.format_exc())
