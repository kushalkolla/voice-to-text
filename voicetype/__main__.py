"""Command-line entry point.

    python -m voicetype                 # run the dictation app (tray + hotkeys)
    python -m voicetype --doctor        # check every subsystem; says READY or what's wrong
    python -m voicetype --selftest      # exercise the text pipeline, no mic needed
    python -m voicetype --check-mic     # record 3 s and verify the mic delivers audio
    python -m voicetype --list-devices  # list microphones
    python -m voicetype --download      # pre-download the speech model
    python -m voicetype --version
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .config import CONFIG_PATH, load_config
from .logging_setup import setup_logging

log = logging.getLogger("voicetype.main")

# Kept alive for the whole process so the single-instance mutex isn't released.
_single_instance_handle = None


def _acquire_single_instance(name: str = "VoiceTypeSingleInstanceMutex") -> bool:
    """Return True if this is the only instance, False if one is already running.

    Uses a named mutex. ``use_last_error`` + ``ctypes.get_last_error()`` is the
    only reliable way to read CreateMutexW's error (a plain ``GetLastError()``
    call can be clobbered by ctypes' own intervening API calls), and setting
    ``restype`` to HANDLE avoids truncating the handle on 64-bit Windows.
    """
    global _single_instance_handle
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        handle = kernel32.CreateMutexW(None, False, name)
        last_error = ctypes.get_last_error()
        ERROR_ALREADY_EXISTS = 183
        if not handle or last_error == ERROR_ALREADY_EXISTS:
            return False
        _single_instance_handle = handle
        return True
    except Exception:  # noqa: BLE001 - non-Windows or restricted: don't block startup
        return True


def _cmd_selftest(cfg: dict) -> int:
    """Run the text pipeline on sample input — verifies cleanup + grammar."""
    from .postprocess import PostProcessor
    from .grammar import GrammarCorrector

    pp = PostProcessor(cfg)
    samples = [
        "um so i think we should uh ship this on friday new line thanks",
        "the meeting is at 3 pm.lets talk about the budget,and the roadmap",
        "i seen the report and its definitely ready for they're review",
    ]
    grammar = GrammarCorrector(cfg)
    print(f"VoiceType {__version__} — pipeline self-test\n" + "=" * 50)
    for s in samples:
        text = pp.pre_grammar(s)
        if grammar.enabled:
            text = grammar.correct(text)
        text = pp.post_grammar(text)
        print(f"\nIN : {s!r}\nOUT: {text!r}")
    grammar.close()
    print("\nGrammar engine available:", grammar.available)
    print("Self-test complete.")
    return 0


def _cmd_list_devices() -> int:
    from .audio import list_input_devices

    devices = list_input_devices()
    if not devices:
        print("No input devices found.")
        return 1
    print("Microphones (use the index or a name substring in config.json):\n")
    for d in devices:
        print(f"  [{d['index']:>2}] {d['name']}  ({d['channels']} ch, {d['sr']} Hz)")
    return 0


def _cmd_check_mic(cfg: dict) -> int:
    """Record a short sample and report whether the mic is actually delivering
    audio — the fast way to tell a working mic from a muted/blocked/wrong one."""
    from .audio import probe_input

    seconds = 3.0
    print(f"VoiceType {__version__} — microphone check")
    print(f"Recording {seconds:.0f}s from the configured input… (just let it run)\n")
    r = probe_input(cfg, seconds=seconds)
    verdict = r.get("verdict")

    if verdict == "error":
        print(f"  Could not open the microphone: {r.get('error')}")
        print("\n  → Another app may hold it exclusively, or no input device exists.")
        print("    Try a different device with `--list-devices`, then set")
        print('    "audio": {"input_device": <index or name>} in config.json.')
        return 1

    print(f"  Device : [{r.get('device_index')}] {r.get('name')}")
    print(f"  Rate   : {r.get('samplerate')} Hz   Samples: {r.get('samples')}")
    print(f"  Peak   : {r.get('peak', 0.0):.5f}   RMS: {r.get('rms', 0.0):.5f}"
          f"   Nonzero: {100 * r.get('nonzero_frac', 0.0):.0f}%")

    if verdict == "ok":
        print("\n  ✓ Mic works — clear audio detected. Dictation should type fine.")
        return 0
    if verdict == "quiet":
        print("\n  ⚠ Mic works but the signal is very faint. Raise the mic level in")
        print("    Windows Sound settings, or move closer. Dictation may still work.")
        return 0
    # silent
    print("\n  ✗ No audio — the mic returned digital silence. Likely causes:")
    print("      • the mic is muted (hardware switch or Windows mixer),")
    print("      • another app is holding it exclusively,")
    print("      • Windows mic privacy is blocking it, or")
    print("      • the wrong input device is selected.")
    print("    Fix the source, or pick another device via `--list-devices` +")
    print('    "audio": {"input_device": …} in config.json, then re-run --check-mic.')
    return 2


def _cmd_doctor(cfg: dict) -> int:
    """Check every subsystem and print a readiness report. Exit 0 if ready
    (no blocking failures), 1 otherwise. This is the one command to run to
    confirm VoiceType is set up correctly on a machine."""
    import importlib
    import platform

    checks: list[tuple[str, str, str]] = []  # (level, label, detail)

    def add(level: str, label: str, detail: str = "") -> None:
        checks.append((level, label, detail))

    print(f"VoiceType {__version__} — system doctor")
    print("=" * 54)
    print("Checking subsystems (this loads the model + grammar, ~10 s)…\n")

    # Python
    add("OK", f"Python {platform.python_version()}", platform.platform())

    # Core dependencies
    deps = [("numpy", "numpy"), ("sounddevice", "sounddevice"), ("soxr", "soxr"),
            ("faster_whisper", "faster-whisper"), ("pynput", "pynput"),
            ("keyboard", "keyboard"), ("pyperclip", "pyperclip"),
            ("pystray", "pystray"), ("PIL", "Pillow")]
    missing = [name for mod, name in deps if not _can_import(importlib, mod)]
    if missing:
        add("FAIL", "Core dependencies",
            "missing: " + ", ".join(missing) + "  (pip install -r requirements.txt)")
    else:
        add("OK", "Core dependencies", f"all {len(deps)} present")

    # Microphone (real capture)
    try:
        from .audio import probe_input

        r = probe_input(cfg, seconds=1.5)
        dev = f"[{r.get('device_index')}] {str(r.get('name', '?'))[:28]}"
        v = r.get("verdict")
        sig = 100 * r.get("nonzero_frac", 0.0)
        if v == "ok":
            add("OK", "Microphone", f"{dev} — clear (peak {r.get('peak', 0):.3f})")
        elif v == "quiet":
            # Alive (nonzero signal) but quiet — almost always just "nobody spoke
            # during the probe", not a real fault, so this still passes.
            add("OK", "Microphone", f"{dev} — alive ({sig:.0f}% signal; quiet during test)")
        elif v == "silent":
            add("FAIL", "Microphone", f"{dev} — digital silence (muted / in use / wrong device)")
        else:
            add("FAIL", "Microphone", f"could not open: {r.get('error')}")
    except Exception as exc:  # noqa: BLE001
        add("FAIL", "Microphone", str(exc))

    # Speech model (and GPU/CPU)
    try:
        from .transcribe import Transcriber

        t = Transcriber(cfg)
        t.ensure_loaded()
        detail = f"{cfg['model']['name']} on {t.device}/{t.compute_type}"
        if t.device == "cuda":
            add("OK", "Speech model — GPU", detail + " (~20x faster)")
        else:
            add("OK", "Speech model — CPU", detail + " (no NVIDIA GPU; still works)")
    except Exception as exc:  # noqa: BLE001
        add("FAIL", "Speech model", str(exc))

    # Grammar (optional)
    try:
        from .grammar import GrammarCorrector

        g = GrammarCorrector(cfg)
        if not g.enabled:
            add("OK", "Grammar", "disabled in config")
        elif g._ensure_tool():  # noqa: SLF001 - internal sync check is intentional here
            add("OK", "Grammar — LanguageTool", "ready (Java found)")
        else:
            add("WARN", "Grammar — LanguageTool",
                "off — install Java (winget install Microsoft.OpenJDK.17); dictation still works")
        g.close()
    except Exception as exc:  # noqa: BLE001
        add("WARN", "Grammar", str(exc))

    # Config & hotkey
    try:
        from .tray import _pretty_hotkey

        hk = _pretty_hotkey(cfg["hotkeys"].get("toggle", ""))
        add("OK", "Config & hotkey", f"toggle = {hk}")
    except Exception as exc:  # noqa: BLE001
        add("WARN", "Config", str(exc))

    # Report
    symbol = {"OK": "[ OK ]", "WARN": "[WARN]", "FAIL": "[FAIL]"}
    for level, label, detail in checks:
        line = f"  {symbol[level]}  {label}"
        if detail:
            line += f"  —  {detail}"
        print(line)

    fails = sum(1 for c in checks if c[0] == "FAIL")
    warns = sum(1 for c in checks if c[0] == "WARN")
    print("\n" + "=" * 54)
    if fails == 0 and warns == 0:
        print("  ✓ READY — everything works. Press your hotkey and talk.")
    elif fails == 0:
        print(f"  ✓ READY — works now, {warns} optional item(s) noted above.")
    else:
        print(f"  ✗ {fails} blocking issue(s) — fix the [FAIL] line(s) above.")
    print(f"\n  Config: {CONFIG_PATH}")
    return 1 if fails else 0


def _can_import(importlib, mod: str) -> bool:
    try:
        importlib.import_module(mod)
        return True
    except Exception:  # noqa: BLE001
        return False


def _cmd_download(cfg: dict) -> int:
    from .transcribe import Transcriber

    print(f"Downloading / loading model {cfg['model']['name']!r} …")
    t = Transcriber(cfg)
    t.ensure_loaded()
    print(f"Model ready (device={t.device}, compute={t.compute_type}).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="voicetype", description="Free offline talk-to-type.")
    parser.add_argument("--version", action="version", version=f"VoiceType {__version__}")
    parser.add_argument("--selftest", action="store_true", help="test the text pipeline")
    parser.add_argument("--doctor", action="store_true",
                        help="check every subsystem and report if VoiceType is ready")
    parser.add_argument("--check-mic", action="store_true",
                        help="record 3 s and verify the mic delivers audio")
    parser.add_argument("--list-devices", action="store_true", help="list microphones")
    parser.add_argument("--download", action="store_true", help="pre-download the speech model")
    parser.add_argument("--config-path", action="store_true", help="print the config file path")
    args = parser.parse_args(argv)

    # Windows consoles default to a legacy code page; force UTF-8 so transcribed
    # text with curly quotes/dashes prints (and logs) cleanly.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    cfg = load_config()
    setup_logging(cfg["general"].get("log_level", "INFO"))

    if args.config_path:
        print(CONFIG_PATH)
        return 0
    if args.list_devices:
        return _cmd_list_devices()
    if args.check_mic:
        return _cmd_check_mic(cfg)
    if args.doctor:
        return _cmd_doctor(cfg)
    if args.selftest:
        return _cmd_selftest(cfg)
    if args.download:
        return _cmd_download(cfg)

    # Default: run the full app — but only one instance at a time (the startup
    # shortcut and a manual launch must not both run and fight over the hotkey).
    if not _acquire_single_instance():
        log.info("VoiceType is already running; exiting this instance.")
        return 0

    from .app import App

    app = App(cfg)
    try:
        app.run()
    except KeyboardInterrupt:
        app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
