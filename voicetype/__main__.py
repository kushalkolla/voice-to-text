"""Command-line entry point.

    python -m voicetype                 # run the dictation app (tray + hotkeys)
    python -m voicetype --selftest      # exercise the text pipeline, no mic needed
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
