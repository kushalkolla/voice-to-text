"""Tiny, non-blocking audio cues for start / stop / error.

Uses the Windows-only :mod:`winsound` module. On any other platform (or if it
is unavailable) the calls become no-ops so nothing crashes.
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

try:
    import winsound  # type: ignore
except Exception:  # noqa: BLE001 - non-Windows or restricted environment
    winsound = None  # type: ignore


def _beep(freq: int, dur_ms: int) -> None:
    if winsound is None:
        return
    try:
        winsound.Beep(int(freq), int(dur_ms))
    except Exception:  # noqa: BLE001 - audio is non-essential
        pass


def _beep_async(freq: int, dur_ms: int) -> None:
    threading.Thread(target=_beep, args=(freq, dur_ms), daemon=True).start()


def play_start(cfg: dict) -> None:
    if cfg.get("ui", {}).get("sounds", True):
        _beep_async(cfg["ui"].get("start_sound_freq", 880), 90)


def play_stop(cfg: dict) -> None:
    if cfg.get("ui", {}).get("sounds", True):
        _beep_async(cfg["ui"].get("stop_sound_freq", 620), 90)


def play_error(cfg: dict) -> None:
    if cfg.get("ui", {}).get("sounds", True):
        # Two quick low beeps read clearly as "something went wrong".
        def _err() -> None:
            _beep(300, 120)
            _beep(240, 160)

        threading.Thread(target=_err, daemon=True).start()
