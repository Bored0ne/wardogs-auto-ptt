# WARDOGS AUTO PTT

Audio-activity push-to-talk for **WarDogs**. Watch the Voicemod virtual mic
(or any mic you pick) and automatically hold the correct chat key whenever you
speak - no push-to-talk key required.

> Works best with a Voicemod virtual microphone so your voice **and** soundboard
> effects both transmit.

## Features

- **Squad vs Local chat** - switch with **F1** (or F11). The **entire UI**
  re-colors when you switch, so you always know which chat you are on at a glance:
  - **Local** is always **orange**.
  - **Squad** uses your team color - pick **red**, **green** or **blue**
    (set `SQUAD_TEAM_COLOR` at the top of the script, or pass `--squad-color`).
- **F2** - mute / unmute (remembers your previous state).
- **F3** (or F9) - pick your input device live.
- **F8** / **Space** - toggle PTT on/off.
- **F10** - rescan audio devices.
- **- / =** - sensitivity down / up.
- **F12** / **q** - quit.
- **Deduplicated device list** - Windows shows the same physical mic 3-4 times
  (MME / DirectSound / WASAPI / WDM-KS); this tool shows each mic once,
  preferring the modern **WASAPI** host API.
- Live audio-level scope + dB meter.

## Download (Windows)

Grab the latest `wardogs-auto-ptt-windows.zip` from the
[Releases](../../releases) page. It contains:

- `wardogs_voice_ptt.exe` - the app (no Python needed)
- `ReadMe.md`

Extract both to a folder and run the `.exe`.

> SmartScreen may warn about an unsigned download. Select "More info" then
> "Run anyway", or build it yourself with the instructions below.

## Usage

```
wardogs_voice_ptt.exe [options]
```

Options:

| Flag | Meaning |
| --- | --- |
| `--list-devices` | print available audio devices and what the picker offers, then exit |
| `--device INDEX_OR_NAME` | skip the picker and use this input directly |
| `--squad-color red\|green\|blue` | squad team color for the UI |
| `--monitor-only` | watch levels without pressing chat keys |
| `--no-color` | disable ANSI colors in `--list-devices` |

On first run you'll be asked to pick your input device. Pick your Voicemod
virtual mic (or any mic). Esc keeps the default.

## Install from source

Requires Python 3.10+.

```
py -m pip install -r requirements.txt
py wardogs_voice_ptt.py
```

## Packaging (contributors)

Build a single-file Windows executable with PyInstaller:

```
py -m pip install -r requirements.txt pyinstaller
pyinstaller --clean --noconfirm wardogs_voice_ptt.spec
```

The `.exe` lands in `dist/wardogs_voice_ptt.exe`.

The spec collects the bundled PortAudio DLLs that `sounddevice` needs, so the
binary is self-contained (no extra DLLs to ship).

### Continuous builds / releases

GitHub Actions builds the Windows executable automatically:

- on a pushed **tag** (`v*`),
- when a **Release** is published,
- or manually via **Actions -> Build & Release -> Run workflow**.

A release asset `wardogs-auto-ptt-windows.zip` is attached to the tag/release.

## Controls summary

| Key | Action |
| --- | --- |
| F1 / F11 | switch chat squad <-> local (whole UI re-colors) |
| F2 | mute / unmute |
| F3 / F9 | pick input device |
| F8 / Space | toggle PTT on/off |
| F10 | rescan devices |
| - / = | sensitivity down / up |
| F12 / q | quit |

## Team colors

Open `wardogs_voice_ptt.py` and set `SQUAD_TEAM_COLOR` to your team color:

```python
SQUAD_TEAM_COLOR = "green"   # "red" | "green" | "blue"
```

or pass it on the command line:

```
wardogs_voice_ptt.exe --squad-color blue
```

## License

Beerware. Do whatever you want with it; if we ever meet and you like it, buy
me a beer. :)
