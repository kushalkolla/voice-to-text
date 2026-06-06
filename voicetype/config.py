"""Configuration: load/merge/save a JSON config with sane defaults.

The config lives at ``<project>/config.json``. Any keys the user omits fall
back to :data:`DEFAULTS`, so partial configs are always valid and upgrades that
add new keys never break an existing install.
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Project root = the folder that contains the ``voicetype`` package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
LOG_DIR = PROJECT_ROOT / "logs"

# Every tunable lives here with a documented default. Editing config.json
# overrides any of these; deleting a key restores the default below.
DEFAULTS: dict[str, Any] = {
    "model": {
        # Whisper model. "auto" (recommended) loads the most accurate free model
        # your hardware can run: large-v3 on an NVIDIA GPU (multilingual, best for
        # names/accents; ~3 GB VRAM + a one-time download), small.en on CPU (fast).
        # Pin one to override: "tiny.en"/"base.en" (faster), "medium.en"/"large-v3"
        # (more accurate), "distil-small.en" (light). Non-".en" = other languages.
        "name": "auto",
        "device": "auto",          # auto | cpu | cuda
        "compute_type": "auto",    # auto | int8 | int8_float16 | float16 | float32
        "beam_size": 5,
        "vad_filter": True,        # trim silence with Silero VAD (cleaner output)
        "language": "en",          # set null for auto-detect with a multilingual model
        "initial_prompt": "",      # optional bias text, e.g. names/jargon you say often
        "temperature": 0.0,
        # 0 = auto (total CPU cores minus two). Leaving cores free keeps the
        # global hotkey and the UI responsive while Whisper is decoding (it also
        # stops Windows' own panel leaking through if you switch to Win+H).
        "cpu_threads": 0,
        # "transcribe" = write what you say. "translate" = speak any language and
        # it types English. Translate needs a multilingual model: also set
        # "name" to "small" (or base/medium) and "language" to null.
        "task": "transcribe",
    },
    "audio": {
        "input_device": None,      # null = system default; or device index / name substring
        # Prefer the WASAPI version of the default mic (Windows' modern shared-mode
        # capture path) over PortAudio's legacy MME default. WASAPI shared mode is
        # how two apps cleanly share one microphone — so this lets VoiceType run
        # alongside another always-on dictation app (e.g. Wispr Flow) and is also
        # lower-latency. Only applies when input_device is null; set false to force
        # the old MME default, or set input_device explicitly to override entirely.
        "prefer_wasapi": True,
        "max_seconds": 120,        # safety cap on a single utterance (push-to-talk mode)
        "min_seconds": 0.25,       # ignore accidental ultra-short taps
        # Silent-mic guard: if the mic delivers digital silence (a stream of exact
        # zeros — muted, held by another app, or wrong device) for this many seconds
        # while you're dictating, the pill warns "No mic input" instead of silently
        # typing nothing. A live mic emits nonzero dither even in a quiet room, so
        # this only fires for a real problem. Set to 0 to disable.
        "silence_warn_seconds": 4.0,
        # Optional gentle noise reduction before recognition (low-rumble high-pass
        # + a conservative spectral gate). Whisper is already noise-robust, so this
        # is off by default; enable it only if you dictate in a consistently noisy
        # room (fan/AC/traffic). Too-aggressive denoising can hurt accuracy.
        "denoise": False,
    },
    "streaming": {
        # Live dictation: press the shortcut once and it types as you speak, one
        # phrase at a time, until you click the ✕ button (or press Esc / the
        # shortcut again). Off = older "record, then transcribe on the 2nd press".
        "enabled": True,
        "silence_ms": 650,         # a pause this long ends a phrase and types it
        "min_phrase_ms": 300,      # ignore speech blips shorter than this
        "max_phrase_ms": 14000,    # force a phrase out so long monologues keep flowing
        "onset_ms": 150,           # speech must persist this long to start a phrase (debounce)
        "start_pad_ms": 200,       # keep this much audio before onset so words aren't clipped
        "energy_floor": 0.003,     # min RMS (0..1) to count as speech; raised automatically in noise
        "energy_mult": 3.0,        # live threshold = max(energy_floor, noise_floor * energy_mult)
        # Hysteresis: once mid-phrase, the bar to *keep* going is this fraction of
        # the bar to start. Soft syllables and brief between-word dips then don't
        # get mistaken for the end of a sentence (fewer phrases cut mid-thought).
        "release_mult": 0.6,
    },
    "hotkeys": {
        # Press this once to start dictating; stop with the ✕ button, Esc, or by
        # pressing it again. Default is the easy bottom-left Left Ctrl + Left Alt.
        # (Heads-up: it shares keys with Ctrl+Alt+… shortcuts, so those can also
        # toggle it. Conflict-free alternatives: "caps lock", "f9", or the
        # right-hand "right ctrl+right shift". "windows+h" uses the Windows key.)
        "toggle": "left ctrl+left alt",
        # Only needed to steal Win+H from Windows; auto-ignored for any other key
        # (suppressing an ordinary key would swallow it from every app).
        "suppress_toggle": True,
        # Tap a neutral key while Win is held so a suppressed Win+H can't leave a
        # lone Win press that pops the Start menu. Only used for Win-based keys.
        "tame_win_key": True,
        # Ignore repeat toggle fires within this many ms of the last one. Stops a
        # modifier combo (Ctrl+Alt) from starting and instantly stopping dictation.
        "toggle_debounce_ms": 500,
        # Optional: hold a single key to talk, release to insert. Empty = off.
        "push_to_talk": "",
        "cancel": "esc",            # stop dictating (keeps what was already typed)
    },
    "grammar": {
        "enabled": True,
        "language": "en-US",
        "picky": False,                       # stricter style suggestions when True
        "disabled_categories": ["TYPOGRAPHY"],  # avoid fighting our own spacing rules
        "disabled_rules": [],
    },
    "postprocess": {
        "remove_fillers": True,
        "fillers": ["um", "uh", "uhh", "uhm", "erm", "hmm", "mhm"],
        "smart_capitalize": True,
        "capitalize_i": True,
        "fix_spacing": True,
        # Turn spoken numbers/symbols into written form: "five dollars" -> "$5",
        # "twenty percent" -> "20%", "example dot com" -> "example.com".
        "format_numbers": True,
        "voice_commands": True,
        # Spoken phrase -> inserted text. Said out loud, these become symbols.
        "commands": {
            "new line": "\n",
            "new paragraph": "\n\n",
            "press enter": "\n",
            "tab key": "\t",
        },
        # Custom dictionary: spoken/misheard form -> how it should be written.
        "replacements": {},
    },
    "injection": {
        "method": "paste",          # paste (fast, reliable) | type (works where paste is blocked)
        "append_space": True,       # add a trailing space so words don't run together
        "restore_clipboard": True,  # put your old clipboard back after pasting
        "paste_delay_ms": 80,
        "key_delay_ms": 2,          # per-character delay in "type" mode
    },
    "ui": {
        "tray_icon": True,
        "show_overlay": True,       # small floating "Listening…" pill near the cursor
        "sounds": True,             # subtle start/stop beeps
        "start_sound_freq": 880,
        "stop_sound_freq": 620,
    },
    "general": {
        "log_level": "INFO",        # DEBUG | INFO | WARNING | ERROR
        "warmup_on_start": True,    # load the model in the background at launch
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` onto a copy of ``base``."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Return the effective config (user file merged over :data:`DEFAULTS`).

    If the file does not exist it is created with the defaults so users have a
    documented starting point to edit.
    """
    path = path or CONFIG_PATH
    if not path.exists():
        save_config(DEFAULTS, path)
        log.info("Created default config at %s", path)
        return copy.deepcopy(DEFAULTS)
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(user, dict):
            raise ValueError("config root must be a JSON object")
        return _deep_merge(DEFAULTS, user)
    except Exception as exc:  # noqa: BLE001 - never let a bad config crash startup
        log.error("Could not read %s (%s); using defaults.", path, exc)
        return copy.deepcopy(DEFAULTS)


def save_config(cfg: dict[str, Any], path: Path | None = None) -> None:
    """Write ``cfg`` to disk as pretty JSON."""
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
