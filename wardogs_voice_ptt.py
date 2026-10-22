"""WARDOGS AUTO PTT - audio-activity push-to-talk for WarDogs.

Watches the Voicemod virtual microphone and holds the selected chat key
whenever audio rises above a threshold. Terminal UI built with Textual.

Controls
--------
  F1 / F11 : switch chat  squad <-> local  (the whole UI re-colors)
  F2       : mute / unmute
  F3 / F9  : pick input device
  F8/Space : toggle PTT on/off
  F10      : rescan devices
  -  /  =  : sensitivity down / up
  F12 / q  : quit

Run
---
  py wardogs_voice_ptt.py
  py wardogs_voice_ptt.py --list-devices
  py wardogs_voice_ptt.py --squad-color red   # or green | blue
"""

import argparse, collections, math, queue, re, sys, threading, time
import numpy as np, sounddevice as sd
from pynput import keyboard
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Footer, Header, OptionList, Static
from rich.text import Text

# ---------------- Configuration ----------------

# Default input device, used to pre-highlight the picker (and as the fallback
# if the picker is cancelled):
#   None        -> show the picker; auto-highlight the first input whose name
#                  contains VOICEMOD_NAME_HINT (or the first input if none)
#   int         -> numeric device index (see --list-devices); skips the picker
#   str         -> name substring, matched case-insensitively; skips the picker
INPUT_DEVICE = None
VOICEMOD_NAME_HINT = "voicemod"

# Chat keys in WarDogs: hold the squad key to talk on squad, hold the local
# key to talk on local. Starts on squad; F1/F11 switches any time.
SQUAD_KEY = "j"
LOCAL_KEY = "k"
START_CHAT = "squad"          # "squad" (team-color UI) or "local" (orange UI)

# Audio activity thresholds in dBFS. A higher number (e.g. -35) is LESS
# sensitive; a lower number (e.g. -55) is MORE sensitive. Start at -42 dBFS
# and adjust live with - / =.
ACTIVATE_DBFS = -42.0
RELEASE_DBFS = -48.0   # hysteresis: once open, stay open down to this level
MIN_ACTIVATE_DBFS = -70.0
MAX_ACTIVATE_DBFS = -20.0

# Require audio above the activation threshold for this long before opening.
ATTACK_MS = 40

# Keep the chat key held this long after audio falls below RELEASE_DBFS.
RELEASE_MS = 280

# Audio capture: 20 ms blocks give responsive activation.
SAMPLE_RATE = 48000
BLOCK_MS = 20
CHANNELS = 1

# How much audio history the right-hand scope keeps visible, in seconds.
SCOPE_SECONDS = 12.0

# UI refresh interval for the numeric meter.
METER_INTERVAL_S = 0.1

# Input-device duplication: Windows exposes the same physical microphone
# through several APIs (MME, DirectSound, WASAPI, WDM-KS), so the raw device
# list shows "the same mic" 3-4 times. We deduplicate clones of the same
# physical mic and only keep ONE device per mic, preferring the modern WASAPI
# host API.  DEDUPE_INPUTS False -> show every index (no filtering at all).
#
# While a device is running, Windows may hold it open and hide it from a
# rescan. If a mic you expect is missing in the picker, use F10 to rescan,
# or start without other apps using the mic.
DEDUPE_INPUTS = True
PREFERRED_HOST_APIS = ("Windows WASAPI", "WDM-KS", "MME", "Windows DirectSound")

# Whole-UI color themes, one per chat. $primary/$accent/$success/etc. drive
# EVERY color on screen (borders, titles, meter, talk state, status bar,
# scope...) so the whole UI changes with the chat (F1).
#
# Squad = YOUR team color (there's a lot of rivalry!): pick red, green or blue
# below.  Local = orange, always.
SQUAD_TEAM_COLOR = "green"     # "red" | "green" | "blue"


def _palette(name, primary, secondary, accent, success,
             bg, surface, panel, boost, fg="#E0E0E0"):
    return dict(
        name=name,
        primary=primary, secondary=secondary,
        warning=accent, error="#E5484D", success=success,
        accent=accent, foreground=fg, background=bg,
        surface=surface, panel=panel, boost=boost,
    )


_SQUAD_PALETTES = {
    "red": dict(
        primary="#E23B3B", secondary="#A52A2A", accent="#FF8A5C",
        success="#FF5A5A", bg="#231314", surface="#2C1A1B",
        panel="#1A0F10", boost="#3C2123",
    ),
    "green": dict(
        primary="#17B26A", secondary="#0B7A48", accent="#7BE3B0",
        success="#3DDC84", bg="#16211B", surface="#1D2A24",
        panel="#101A15", boost="#2C3A33",
    ),
    "blue": dict(
        primary="#2F7FE0", secondary="#1B56A6", accent="#7FB8F5",
        success="#5FA8F5", bg="#121A26", surface="#192233",
        panel="#0D141F", boost="#26364D",
    ),
}


def _build_squad_theme():
    key = SQUAD_TEAM_COLOR if SQUAD_TEAM_COLOR in _SQUAD_PALETTES else "green"
    c = _SQUAD_PALETTES[key]
    return _palette(
        "wardogs-squad", c["primary"], c["secondary"], c["accent"],
        c["success"], c["bg"], c["surface"], c["panel"], c["boost"],
    )


SQUAD_THEME = _build_squad_theme()
LOCAL_THEME = _palette(
    "wardogs-local",
    primary="#FF8A00", secondary="#C46900", accent="#FFB84D",
    success="#FF9A2E", bg="#221809", surface="#2B1D0C",
    panel="#1A1206", boost="#3A2A12", fg="#F5E9D6",
)

# ------------------------------------------------

audio_queue = queue.Queue(maxsize=400)
command_queue = queue.Queue(maxsize=32)
keyboard_controller = None
menu_active = threading.Event()

# Mutable state, all owned by the UI thread.
_enabled = True
muted_saved = True         # _enabled value stashed by F2 mute
talk_down = False
chat_key = LOCAL_KEY if START_CHAT == "local" else SQUAD_KEY
chat_vein = START_CHAT
device_index = 0
device_name = "?"
gate = None
listener = None

# ---------------- Input-device listing / dedupe ----------------

def _norm_device_name(name):
    """Normalize a device name so duplicate clones collapse to one key."""
    # Keep the descriptive part (e.g. "NexiGo HelloCam") and drop only
    # Windows driver/endpoint suffixes, e.g. the trailing "(... Wave)" or
    # the WDM-KS "Wave" token, so "Mic (Steam Streaming Microphone Wave)"
    # and "Mic (Steam Streaming Microphone)" collapse to the same key.
    n = name.strip()
    # Remove a trailing parenthetical only if it is a pure suffix like Wave
    # (drop the "Steam Streaming Microphone Wave" form down to the base name).
    n = re.sub(r"\s*\(([^()]*\bwave\b[^()]*)\)$", "", n, flags=re.I).strip()
    n = re.sub(r"\bwave\b", "", n, flags=re.I).strip()
    n = re.sub(r"[\s\-]+$", "", n).strip()
    n = re.sub(r"\s{2,}", " ", n).strip()
    return n


def api_rank(api_name, preferred=PREFERRED_HOST_APIS):
    """Order host APIs from best (0) to worst; unknown APIs rank worst."""
    try:
        return preferred.index(api_name)
    except ValueError:
        return len(preferred)


def _physical_key(i, dev):
    """Tuple used to decide two listings are the same physical mic."""
    return (_norm_device_name(dev["name"]).casefold(),)


def _dedupe(raw):
    """Collapse duplicate clones of the same physical mic, keep best API."""
    best = {}
    for i, d, api in raw:
        key = _physical_key(i, d)
        if key not in best:
            best[key] = (i, d, api)
        else:
            if api_rank(api) < api_rank(best[key][2]):
                best[key] = (i, d, api)
    items = sorted(best.values(), key=lambda r: (api_rank(r[2]), r[0]))
    return [(i, d["name"]) for i, d, _a in items]


def list_inputs(dedupe=None):
    """Return [(index, name), ...] for usable inputs.

    dedupe=True (default): collapse duplicate clones of the same physical mic,
    keeping the best (preferred host-API) index.
    dedupe=False: show every index (no filtering at all).
    """
    if dedupe is None:
        dedupe = DEDUPE_INPUTS
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()
    raw = [
        (i, d, hostapis[d["hostapi"]]["name"])
        for i, d in enumerate(devices)
        if d.get("max_input_channels", 0) > 0
    ]
    if not dedupe:
        return [(i, d["name"]) for i, d, _a in raw]
    return _dedupe(raw)




def device_name_of(index):
    try:
        return sd.query_devices(index)["name"]
    except Exception:
        return f"device {index}"


def resolve_default_input(inputs):
    if not inputs:
        raise RuntimeError("No audio input devices were found on this system.")
    if isinstance(INPUT_DEVICE, int):
        for i, _name in inputs:
            if i == INPUT_DEVICE:
                return i
        raise RuntimeError(
            f"INPUT_DEVICE index {INPUT_DEVICE} was not found among input "
            f"devices: {", ".join(str(i) for i, _ in inputs)}"
        )
    if isinstance(INPUT_DEVICE, str):
        for i, name in inputs:
            if INPUT_DEVICE.casefold() in name.casefold():
                return i
        raise RuntimeError(
            f"INPUT_DEVICE {INPUT_DEVICE!r} matched no input device name."
        )
    for i, name in inputs:
        if VOICEMOD_NAME_HINT.casefold() in name.casefold():
            return i
    return inputs[0][0]


def resolve_cli_device(inputs, spec):
    if spec.isdigit():
        idx = int(spec)
        for i, _name in inputs:
            if i == idx:
                return idx
        raise RuntimeError(f"--device index {idx} was not found among inputs.")
    for i, name in inputs:
        if spec.casefold() in name.casefold():
            return i
    raise RuntimeError(f"--device {spec!r} matched no input device name.")


# ---------------- Audio / state machine ----------------

class ActivityGate:
    """Attack / release state machine for audio-activity gating."""

    def __init__(self, activate_dbfs, release_dbfs, attack_ms, release_ms):
        self.set_thresholds(activate_dbfs, release_dbfs)
        self.attack_ms = attack_ms
        self.release_ms = release_ms
        self.talking = False
        self.attack_started = None
        self.below_since = None

    def set_thresholds(self, activate_dbfs, release_dbfs=None):
        if release_dbfs is None:
            release_dbfs = activate_dbfs - 6.0
        self.activate_dbfs = activate_dbfs
        self.release_dbfs = min(release_dbfs, activate_dbfs - 1.0)

    def reset(self):
        self.talking = False
        self.attack_started = None
        self.below_since = None

    def feed(self, level, now):
        """Advance the gate with a new level; returns open/close/empty."""
        if not self.talking:
            if level >= self.activate_dbfs:
                if self.attack_started is None:
                    self.attack_started = now
                elif (now - self.attack_started) * 1000.0 >= self.attack_ms:
                    self.talking = True
                    self.attack_started = None
                    self.below_since = None
                    return "open"
            else:
                self.attack_started = None
        else:
            if level >= self.release_dbfs:
                self.below_since = None
            else:
                if self.below_since is None:
                    self.below_since = now
                elif (now - self.below_since) * 1000.0 >= self.release_ms:
                    self.talking = False
                    self.attack_started = None
                    self.below_since = None
                    return "close"
        return ""


class InputMonitor:
    """Owns the sounddevice InputStream for the selected input device."""

    def __init__(self, index):
        self.index = index
        self.stream = None

    def start(self):
        # Some devices (especially WASAPI/WDM-KS mics) reject 48 kHz. Try the
        # configured rate first, then fall back to the device default rate.
        rates = [SAMPLE_RATE]
        try:
            default_rate = int(sd.query_devices(self.index)["default_samplerate"])
            if default_rate not in rates:
                rates.append(default_rate)
        except Exception:
            pass
        last_exc = None
        for rate in rates:
            try:
                self.stream = sd.InputStream(
                    device=self.index,
                    samplerate=rate,
                    blocksize=int(rate * BLOCK_MS / 1000),
                    channels=CHANNELS,
                    dtype="float32",
                    latency="low",
                    callback=audio_callback,
                )
                self.stream.start()
                self.samplerate = rate
                return
            except Exception as exc:
                last_exc = exc
        raise last_exc

    def stop(self):
        if self.stream is not None:
            for op in ("stop", "close"):
                try:
                    getattr(self.stream, op)()
                except Exception:
                    pass
            self.stream = None


def audio_callback(indata, frames, callback_time, status):
    if status:
        pass  # transient backend warnings are ignored; the scope just skips
    try:
        audio_queue.put_nowait(indata[:, 0].copy())
    except queue.Full:
        pass


def flush_audio():
    while True:
        try:
            audio_queue.get_nowait()
        except queue.Empty:
            return


def rms_dbfs(samples):
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    return 20.0 * math.log10(max(rms, 1e-8))


# ---------------- Talk / commands ----------------

def queue_command(name):
    try:
        command_queue.put_nowait(name)
    except queue.Full:
        pass


def on_key_press(key):
    if menu_active.is_set():
        return
    if key == keyboard.Key.f1:
        queue_command("chat")
    elif key == keyboard.Key.f2:
        queue_command("mute")
    elif key == keyboard.Key.f3:
        queue_command("device_menu")
    elif key == keyboard.Key.f8:
        queue_command("toggle")
    elif key == keyboard.Key.f9:
        queue_command("device_menu")
    elif key == keyboard.Key.f10:
        queue_command("rescan")
    elif key == keyboard.Key.f11:
        queue_command("chat")
    elif key == keyboard.Key.f12:
        queue_command("quit")
        return False


def fail_keyboard(exc):
    """Fail safe: stop auto-PTT and report rather than leaving a key stuck."""
    global _enabled, talk_down
    _enabled = False
    talk_down = False


def set_talk(down):
    """Press (or release) the currently selected chat key."""
    global talk_down
    if keyboard_controller is None:
        talk_down = False
        return
    if not _enabled:
        down = False
    if down == talk_down:
        return
    try:
        if down:
            keyboard_controller.press(chat_key)
            talk_down = True
        else:
            keyboard_controller.release(chat_key)
            talk_down = False
    except Exception as exc:
        fail_keyboard(exc)


def toggle_enabled():
    global _enabled
    _enabled = not _enabled
    if not _enabled and talk_down:
        set_talk(False)
    return _enabled


def set_muted(muted):
    """Mute (True) or unmute (False); remembers the previous PTT state."""
    global _enabled, muted_saved
    if muted:
        if _enabled:
            muted_saved = True
            _enabled = False
    else:
        _enabled = muted_saved
    if not _enabled and talk_down:
        set_talk(False)
    return _enabled


def switch_chat():
    """Swap the chat vein between squad (j) and local (k), anytime."""
    global chat_key, chat_vein
    old = chat_key
    chat_key = LOCAL_KEY if old == SQUAD_KEY else SQUAD_KEY
    chat_vein = "local" if chat_key == LOCAL_KEY else "squad"
    if talk_down and chat_key != old:
        try:
            keyboard_controller.release(old)
            keyboard_controller.press(chat_key)
        except Exception as exc:
            fail_keyboard(exc)
            return
    return chat_key


def adjust_sensitivity(delta):
    global ACTIVATE_DBFS, RELEASE_DBFS, gate
    ACTIVATE_DBFS = max(
        MIN_ACTIVATE_DBFS, min(MAX_ACTIVATE_DBFS, ACTIVATE_DBFS + delta)
    )
    RELEASE_DBFS = ACTIVATE_DBFS - 6.0
    if gate is not None:
        gate.set_thresholds(ACTIVATE_DBFS, RELEASE_DBFS)
    return ACTIVATE_DBFS


# ---------------- Textual widgets ----------------


def _theme_attr(theme, key):
    """Pull a color value from a Theme object or a plain dict."""
    if isinstance(theme, dict):
        return theme[key]
    return getattr(theme, key)


def make_scope_styles(theme):
    """Return the scope character->Rich style map for a chat theme."""
    bg = _theme_attr(theme, "background")
    surface = _theme_attr(theme, "surface")
    primary = _theme_attr(theme, "primary")
    success = _theme_attr(theme, "success")
    warning = _theme_attr(theme, "warning")
    error = _theme_attr(theme, "error")
    return {
        " ": f"dim on {bg}",
        ":": f"dim {success} on {surface}",
        "#": f"{success} on {surface}",
        "+": f"bold {warning} on {surface}",
        "!": f"bold {error} on {surface}",
        "-": f"dim {primary} on {surface}",
    }

class Scope(Static):
    """Scrolling dBFS volume-history scope (hospital pulse / o-scope look)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.history = collections.deque(
            maxlen=max(50, int(SCOPE_SECONDS * 1000.0 / BLOCK_MS))
        )
        self.activate = ACTIVATE_DBFS
        self._cached = None
        self._styles = make_scope_styles(SQUAD_THEME)
        self._theme = "wardogs-squad"

    def set_theme_styles(self, theme_name, theme):
        self._theme = theme_name
        self._styles = make_scope_styles(theme)
        self._cached = None
        self.refresh()

    def add_level(self, level):
        self.history.append(level)
        self._cached = None

    def _lines(self):
        h = max(3, self.content_size.height)
        w = max(8, self.content_size.width)
        rows = h - 2          # top label row + bottom baseline row
        cols = w - 6          # left 5 cols are dB labels
        top_db, bot_db = 0.0, -60.0
        grid = [[" " for _ in range(w)] for _ in range(h)]
        hist = list(self.history)
        n = len(hist)
        for c in range(cols):
            frac = (c + 1) / cols
            idx = int(frac * n) - 1 if n else -1
            idx = max(0, min(n - 1, idx))
            level = hist[idx] if n else bot_db
            r = (level - bot_db) / (top_db - bot_db)
            r = max(0.0, min(1.0, r))
            y = rows - 1 - int(round((rows - 1) * r))
            col = 6 + c
            marker = "!"
            if level < -6.0:
                marker = "+" if level >= self.activate else "#"
            grid[y][col] = marker
            for yy in range(y + 1, rows):
                grid[yy][col] = ":"
        for i, label in enumerate(("0", "-30", "-60")):
            y = (rows - 1) * i // 2
            if y < h:
                for j, ch in enumerate(label):
                    if j < 5:
                        grid[y][1 + j] = ch
        for x in range(5, w):
            grid[rows - 1][x] = "-"
        for j, ch in enumerate("VOLHIST"):
            grid[0][j] = ch
        return ["".join(row) for row in grid]

    def render(self):
        if self._cached is None or self.activate != getattr(self, "_act", None):
            self._cached = self._lines()
            self._act = self.activate
        from rich.text import Text
        t = Text()
        for line in self._cached:
            for ch in line:
                t.append(ch, self._styles.get(ch, ""))
            t.append("\n")
        return t


class DevicePickerScreen(ModalScreen):
    """Modal list of input devices, navigable with arrow keys."""

    BINDINGS = [
        Binding("up,k", "cursor_up", "Up"),
        Binding("down,j", "cursor_down", "Down"),
        Binding("enter,space", "select", "Select"),
        Binding("escape,q", "cancel", "Cancel"),
    ]

    def __init__(self, inputs, default_idx, title="SELECT INPUT DEVICE"):
        super().__init__()
        self.inputs = inputs
        self.default_idx = default_idx
        self.title_text = title

    def compose(self) -> ComposeResult:
        yield Static(self.title_text, id="picker-title")
        ol = OptionList(
            *[f"[{idx:2}] {name}" for idx, name in self.inputs],
            id="picker-list",
        )
        ol.border_title = "input devices"
        yield ol

    def on_mount(self) -> None:
        list_w = self.query_one("#picker-list", OptionList)
        for i, (idx, _name) in enumerate(self.inputs):
            if idx == self.default_idx:
                list_w.highlighted = i
                break
        list_w.focus()

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        idx = self.inputs[event.option_index][0]
        self.dismiss(idx)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------- Textual app ----------------

APP_CSS = """
Screen {
    layout: vertical;
}

#main {
    height: 1fr;
}

#left {
    width: 34%;
    min-width: 30;
    height: 100%;
    border: round $primary;
    padding: 0 1;
    background: $surface;
}

#right {
    width: 66%;
    height: 100%;
    border: round $primary;
    background: $surface;
}

#scope {
    height: 1fr;
    border: round $accent;
}

/* ---------- chat-theme color classes (F1 switches the whole UI) ---------- */

#chat-title, #device-title, #level-title {
    color: $accent;
    text-style: bold;
}

#vein {
    color: $text;
    text-style: bold;
}

#vein.chat-squad {
    color: $success;
    text-style: bold;
}

#vein.chat-local {
    color: $text-accent;
    text-style: bold;
}

#talk {
    color: $text;
}

#talk.talking {
    color: $success;
    text-style: bold;
}

#talk.muted {
    color: $warning;
    text-style: bold;
}

#talk.talking.muted {
    color: $error;
    text-style: bold;
}

#enabled {
    color: $success;
    text-style: bold;
}

#enabled.disabled {
    color: $error;
}

#meter {
    height: 3;
}

#scope-label {
    color: $text-muted;
}

#status {
    height: 1;
    background: $surface;
    color: $text;
    text-style: bold;
}

#status.ok { background: $success; color: $text; }
#status.warn { background: $warning; color: $text; }
#status.err { background: $error; color: $text; }

/* Level/meter readout colors (green/yellow/red), independent of chat theme */
#level-num.level-ok   { color: #3DDC84; }
#level-num.level-warm { color: #FFC73C; }
#level-num.level-hot  { color: #FF4D4D; }
#meter.level-ok   { color: #3DDC84; }
#meter.level-warm { color: #FFC73C; }
#meter.level-hot  { color: #FF4D4D; }

#picker-title {
    color: $accent;
    text-style: bold;
    padding: 1 2;
}

#picker-list {
    border: round $primary;
    margin: 0 2 2 2;
}
"""


def _meter_bar(level, width=20):
    """ASCII bar from -60..0 dBFS, `#` for above-activation segments."""
    frac = (level + 60.0) / 60.0
    frac = max(0.0, min(1.0, frac))
    full = int(round(frac * width))
    return "#" * full + "." * max(0, width - full)


def _level_color(level, activate):
    """Return the level class: ok (green), warm (yellow) or hot (red)."""
    if level >= -6.0:
        return "hot"
    if level >= activate:
        return "warm"
    return "ok"


class WardogsApp(App):
    TITLE = "WARDOGS AUTO PTT"
    SUB_TITLE = "auto push-to-talk"
    CSS = APP_CSS

    BINDINGS = [
        Binding("f1", "chat", "Chat: squad/local"),
        Binding("f2", "mute", "Mute"),
        Binding("f3", "devices", "Input device"),
        Binding("f8,space", "toggle", "PTT on/off"),
        Binding("f9", "devices", "Input device"),
        Binding("f10", "rescan", "Rescan devices"),
        Binding("f11", "chat", "Switch chat"),
        Binding("minus,underscore", "sensitive_down", "Sensitivity -"),
        Binding("equal,plus", "sensitive_up", "Sensitivity +"),
        Binding("f12,q", "quit", "Quit"),
    ]

    def __init__(self, monitor_only, cli_device):
        super().__init__()
        self.monitor_only = monitor_only
        self.cli_device = cli_device
        self.monitor = None
        self.status_kind = "ok"
        self.status_text = "starting..."
        self._last_level = -120.0
        self._need_status_update = True
        self._need_setup = True
        self.inputs = []
        self._themes = {
            "wardogs-squad": Theme(**SQUAD_THEME),
            "wardogs-local": Theme(**LOCAL_THEME),
        }
        self.register_theme(self._themes["wardogs-squad"])
        self.register_theme(self._themes["wardogs-local"])

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static("CHAT", id="chat-title")
                yield Static("SQUAD (j)", id="vein")
                yield Static("idle", id="talk")
                yield Static("INPUT DEVICE", id="device-title")
                yield Static("", id="device-name")
                yield Static("", id="enabled")
                yield Static("LEVEL", id="level-title")
                yield Static("-120.0 dBFS", id="level-num")
                yield Static("", id="meter")
                yield Static("", id="thresholds")
            with Vertical(id="right"):
                yield Scope(id="scope")
                yield Static("0 dB ... -60 dB", id="scope-label")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.theme = "wardogs-squad" if chat_vein == "squad" else "wardogs-local"
        self.set_interval(
            METER_INTERVAL_S,
            self._process_audio,
            name="audio-pump",
        )
        self.call_after_refresh(self._setup)

    def watch_theme(self, old_theme: str, new_theme: str) -> None:
        """Re-render the scope when the chat theme switches."""
        theme = self._themes.get(new_theme)
        if theme is not None:
            try:
                self.query_one("#scope", Scope).set_theme_styles(
                    new_theme, theme
                )
            except Exception:
                pass

    # ---- audio pump / state sync (UI thread, no locks needed) ----

    def _process_audio(self) -> None:
        if self._need_setup:
            return
        now = time.monotonic()
        for _ in range(120):
            try:
                samples = audio_queue.get_nowait()
            except queue.Empty:
                break
            level = rms_dbfs(samples)
            self._last_level = level
            scope = self.query_one("#scope", Scope)
            scope.add_level(level)
            if not _enabled:
                gate.reset()
                continue
            event = gate.feed(level, now)
            if event == "open" and keyboard_controller is not None:
                set_talk(True)
            elif event == "close":
                set_talk(False)
        self._sync_ui(now)

    def _sync_ui(self, now: float) -> None:
        try:
            if chat_vein == "squad":
                vein_text = f"SQUAD ({SQUAD_KEY.upper()})"
                vein_cls = "chat-squad"
            else:
                vein_text = f"LOCAL ({LOCAL_KEY.upper()})"
                vein_cls = "chat-local"
            vein_w = self.query_one("#vein", Static)
            vein_w.update(vein_text)
            vein_w.set_classes(vein_cls)
            talk_w = self.query_one("#talk", Static)
            if talk_down:
                talk_text = "TALKING"
                talk_cls = "talking"
            elif not _enabled:
                talk_text = "MUTED"
                talk_cls = "muted"
            else:
                talk_text = "idle"
                talk_cls = ""
            talk_w.update(talk_text)
            talk_w.set_classes(talk_cls)
            level_cls = _level_color(self._last_level, gate.activate_dbfs)
            level_w = self.query_one("#level-num", Static)
            level_w.update(f"{self._last_level:6.1f} dBFS")
            level_w.set_classes(level_cls)
            meter_w = self.query_one("#meter", Static)
            meter_w.update(_meter_bar(self._last_level))
            meter_w.set_classes(level_cls)
            thr = self.query_one("#thresholds", Static)
            thr.update(
                f"act {gate.activate_dbfs:+.0f} / rel {gate.release_dbfs:+.0f} dB   [ - / + ]"
            )
            self.query_one("#scope", Scope).refresh()
            if self._need_status_update:
                self._need_status_update = False
                st = self.query_one("#status", Static)
                st.update(self.status_text)
                st.set_classes(self.status_kind)
        except Exception:
            pass

    # ---- commands from the global keyboard hook (F1/F2/F3/F8-F12) ----

    def _drain_commands(self) -> None:
        while True:
            try:
                cmd = command_queue.get_nowait()
            except queue.Empty:
                break
            if cmd == "toggle":
                self.action_toggle()
            elif cmd == "mute":
                self.action_mute()
            elif cmd == "chat":
                self.action_chat()
            elif cmd == "device_menu":
                self.action_devices()
            elif cmd == "rescan":
                self.action_rescan()
            elif cmd == "quit":
                self.exit()

    def on_idle(self) -> None:
        self._drain_commands()

    # ---- actions ----

    def action_toggle(self) -> None:
        toggle_enabled()
        self.status_kind = "ok" if _enabled else "warn"
        self.status_text = "PTT enabled" if _enabled else "PTT disabled"
        self._need_status_update = True
        gate.reset()
        self._sync_ui(time.monotonic())

    def action_mute(self) -> None:
        state = set_muted(_enabled)
        self.status_kind = "warn" if not _enabled else "ok"
        self.status_text = ("MUTED (F2 to unmute)" if not _enabled else "Unmuted")
        self._need_status_update = True
        gate.reset()
        self._sync_ui(time.monotonic())

    def action_chat(self) -> None:
        key = switch_chat()
        vein = "squad" if key == SQUAD_KEY else "local"
        self.theme = "wardogs-squad" if vein == "squad" else "wardogs-local"
        self.status_kind = "info"
        self.status_text = f"Chat: {vein} ({key.upper()!r})"
        self._need_status_update = True
        self._sync_ui(time.monotonic())

    def action_sensitive_down(self) -> None:
        self._adjust_sens(-1.0)

    def action_sensitive_up(self) -> None:
        self._adjust_sens(1.0)

    def _adjust_sens(self, delta: float) -> None:
        act = adjust_sensitivity(delta)
        self.status_kind = "info"
        self.status_text = f"Sensitivity: activate {act:+.0f} dBFS  ( - / + )"
        self._need_status_update = True
        self.query_one("#scope", Scope).activate = ACTIVATE_DBFS
        self._sync_ui(time.monotonic())

    def action_devices(self) -> None:
        if not self.inputs:
            self.action_rescan()
            return
        self._open_picker("SWITCH INPUT DEVICE")

    def action_rescan(self) -> None:
        """Re-enumerate devices (applies dedupe defaults)."""
        global device_index
        self.inputs = list_inputs()
        if device_index not in [i for i, _n in self.inputs]:
            device_index = self.inputs[0][0] if self.inputs else 0
        self.status_kind = "info"
        self.status_text = (
            f"Devices rescanned: {len(self.inputs)} input(s) available"
        )
        self._need_status_update = True
        self._sync_ui(time.monotonic())


    def _open_picker(self, title: str) -> None:
        menu_active.set()
        try:
            def on_dismiss(result):
                menu_active.clear()
                if result is None:
                    return
                self._switch_device(result)

            self.push_screen(
                DevicePickerScreen(self.inputs, device_index, title),
                on_dismiss,
            )
        except Exception:
            menu_active.clear()
            raise

    def _switch_device(self, index: int) -> None:
        global device_index, device_name
        if index == device_index:
            self.status_kind = "info"
            self.status_text = f"Already on [{index}] {device_name}"
            self._need_status_update = True
            return
        try:
            self.monitor.stop()
        except Exception:
            pass
        flush_audio()
        gate.reset()
        device_index = index
        device_name = device_name_of(index)
        self.monitor = InputMonitor(index)
        try:
            self.monitor.start()
            self.status_kind = "ok"
            self.status_text = f"Device: [{index}] {device_name}"
            self.query_one("#device-name", Static).update(device_name)
            self.query_one("#enabled", Static).update(
                f"[{device_index}]  {"ENABLED" if _enabled else "DISABLED"}"
            )
        except Exception as exc:
            self.status_kind = "err"
            self.status_text = f"Device switch failed: {exc}"
        self._need_status_update = True
        self._sync_ui(time.monotonic())

    # ---- setup / teardown ----

    def _setup(self) -> None:
        """Determine the input and open the stream after the UI is mounted."""
        global device_index, device_name, gate, keyboard_controller, listener, _enabled
        self.inputs = list_inputs()
        if not self.inputs:
            self._fail_setup("No audio input devices were found on this system.")
            return
        default_idx = resolve_default_input(self.inputs)
        if self.cli_device is not None:
            index = resolve_cli_device(self.inputs, self.cli_device)
        elif INPUT_DEVICE is not None:
            index = default_idx
        elif len(self.inputs) > 1:
            # Ask the user; continue only after they choose (or cancel with Esc).
            self._open_startup_picker(self.inputs, default_idx)
            return
        else:
            index = default_idx
        self._start_audio(index)

    def _open_startup_picker(self, inputs, default_idx: int) -> None:
        """Push the startup device picker; _start_audio follows on dismiss."""
        from textual.screen import Screen

        class _Startup(Screen):
            def __init__(self, inputs, default_idx):
                super().__init__()
                self.inputs = inputs
                self.default_idx = default_idx

            def compose(self):
                yield Static(
                    "SELECT INPUT DEVICE   (arrows choose, Enter accept, "
                    "Esc = default)",
                    id="picker-title",
                )
                ol = OptionList(
                    *[f"[{idx:2}] {name}" for idx, name in self.inputs],
                    id="picker-list",
                )
                ol.border_title = "input devices"
                yield ol

            BINDINGS = [
                Binding("escape", "cancel", "Cancel"),
            ]

            def action_cancel(self) -> None:
                self.dismiss(None)


            def on_mount(self):
                self.call_after_refresh(self._preselect)

            def _preselect(self):
                ol = self.query_one("#picker-list", OptionList)
                for i, (idx, _name) in enumerate(self.inputs):
                    if idx == self.default_idx:
                        ol.highlighted = i
                        break
                ol.focus()

            @on(OptionList.OptionSelected)
            def _sel(self, event):
                self.dismiss(self.inputs[event.option_index][0])

        def _on_dismiss(result):
            chosen = result if result is not None else default_idx
            self._start_audio(chosen)

        self.push_screen(_Startup(inputs, default_idx), _on_dismiss)

    def _fail_setup(self, message: str) -> None:
        self.status_kind = "err"
        self.status_text = message
        self._need_status_update = True
        self._need_setup = False
        self._sync_ui(time.monotonic())

    def _start_audio(self, index: int) -> None:
        global device_index, device_name, gate, keyboard_controller, listener, _enabled
        if self._need_setup is False:
            return  # already started (e.g. duplicate callback)
        device_index = index
        device_name = device_name_of(index)
        gate = ActivityGate(ACTIVATE_DBFS, RELEASE_DBFS, ATTACK_MS, RELEASE_MS)

        self.monitor = InputMonitor(index)
        try:
            self.monitor.start()
        except Exception as exc:
            self._fail_setup(
                f"Could not open input [{index}] {device_name}: {exc}"
            )
            return

        if self.monitor_only:
            keyboard_controller = None
            self.status_kind = "warn"
            self.status_text = "MONITOR ONLY - chat keys will not be pressed"
        else:
            _enabled = True
            try:
                keyboard_controller = keyboard.Controller()
            except Exception as exc:
                keyboard_controller = None
                self.status_kind = "warn"
                self.status_text = (
                    f"Keyboard control unavailable ({exc}) - monitor only"
                )
            else:
                try:
                    listener = keyboard.Listener(on_press=on_key_press)
                    listener.start()
                except Exception as exc:
                    listener = None
                    self.status_kind = "warn"
                    self.status_text += " | global keys off; use terminal keys"
                else:
                    self.status_kind = "ok"
                    self.status_text = f"Listening: [{index}] {device_name}"

        self.query_one("#device-name", Static).update(device_name)
        self.query_one("#enabled", Static).update(
            f"[{device_index}]  {"ENABLED" if _enabled else "DISABLED"}"
        )
        self._need_status_update = True
        self._need_setup = False
        self._sync_ui(time.monotonic())

    def on_unmount(self) -> None:
        if self.monitor is not None:
            try:
                self.monitor.stop()
            except Exception:
                pass


# ---------------- Plain-text helpers ----------------

def list_devices_color(no_color):
    def esc(code):
        return "" if no_color else f"\x1b[{code}m"

    _R = esc("0")
    _B = esc("1")
    _C = esc("36")
    _G = esc("32")
    print(f"\n{_B}{_C}Audio devices{_R} (inputs have in > 0):")
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        inputs_all = [
            (i, d) for i, d in enumerate(devices)
            if d["max_input_channels"] > 0
        ]
        for i, dev in inputs_all:
            api = hostapis[dev["hostapi"]]["name"]
            print(
                f"  [{i:2}] {dev["name"]} - {api}"
                f" ({dev["max_input_channels"]} in, "
                f"{dev["max_output_channels"]} out) "
                f"norm={_norm_device_name(dev["name"])!r}"
            )
    except Exception as exc:
        print(f"  Could not enumerate devices: {exc}")
    print()

    # Show what dedupe would actually offer in the picker.
    print(f"{_B}{_C}Picker will offer{_R} ({len(list_inputs())} input(s)):")
    for idx, name in list_inputs():
        print(f"  {_G}[{idx:2}]{_R} {name}")
    print()

def main():
    parser = argparse.ArgumentParser(
        description="Wardogs Auto PTT - audio-activity push-to-talk (Textual UI)"
    )
    parser.add_argument(
        "--list-devices", action="store_true",
        help="list audio devices and exit",
    )
    parser.add_argument(
        "--device", metavar="INDEX_OR_NAME", default=None,
        help="input device index or name substring; skips the picker",
    )
    parser.add_argument(
        "--monitor-only", action="store_true",
        help="watch levels without pressing chat keys",
    )
    parser.add_argument(
        "--no-color", action="store_true",
        help="disable ANSI colors in --list-devices output",
    )
    parser.add_argument(
        "--squad-color", choices=("red", "green", "blue"), default=None,
        help="squad team color for the UI (overrides SQUAD_TEAM_COLOR)",
    )
    args = parser.parse_args()

    if args.list_devices:
        list_devices_color(args.no_color)
        return

    global SQUAD_TEAM_COLOR, SQUAD_THEME
    if args.squad_color is not None:
        SQUAD_TEAM_COLOR = args.squad_color
        SQUAD_THEME = _build_squad_theme()
    try:
        app = WardogsApp(args.monitor_only, args.device)
        app.run()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        raise
    finally:
        set_talk(False)


if __name__ == "__main__":
    main()

