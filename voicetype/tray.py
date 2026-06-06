"""System-tray icon: live status + a small control menu.

The icon colour reflects the current state (idle / recording / transcribing /
loading / error). The menu lets you toggle grammar, open the config and log
folder, and quit — all without a console window.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from .config import CONFIG_PATH, LOG_DIR

log = logging.getLogger(__name__)

_COLORS = {
    "loading": (255, 184, 0),
    "idle": (86, 156, 104),
    "recording": (222, 45, 45),
    "transcribing": (52, 130, 226),
    "error": (200, 30, 30),
    "paused": (130, 130, 130),
}
_LABELS = {
    "loading": "Loading model…",
    "idle": "Ready - press your shortcut to dictate",
    "recording": "Listening…",
    "transcribing": "Transcribing…",
    "error": "Error (see logs)",
    "paused": "Paused",
}


def _pretty_hotkey(spec: str) -> str:
    """'left ctrl+left alt' -> 'Left Ctrl + Left Alt' for display."""
    parts = [p.strip() for p in (spec or "").split("+") if p.strip()]
    return " + ".join(p.title() for p in parts) or "your shortcut"


def _make_image(color):
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, size - 6, size - 6), fill=color + (255,))
    # A little inner microphone-ish dot for character.
    d.ellipse((24, 18, 40, 38), fill=(255, 255, 255, 235))
    d.rectangle((30, 36, 34, 48), fill=(255, 255, 255, 235))
    d.rectangle((24, 46, 40, 50), fill=(255, 255, 255, 235))
    return img


class Tray:
    def __init__(self, app) -> None:
        self.app = app
        self.state = "loading"
        self._icon = None
        toggle = app.cfg.get("hotkeys", {}).get("toggle", "left ctrl+left alt")
        self._labels = dict(_LABELS)
        self._labels["idle"] = f"Ready - press {_pretty_hotkey(toggle)} to dictate"

    def _build(self):
        import pystray
        from pystray import Menu, MenuItem

        def status_text(_item):
            return f"VoiceType — {self._labels.get(self.state, self.state)}"

        def grammar_checked(_item):
            return self.app.grammar.enabled

        def translate_checked(_item):
            return self.app.transcriber.task == "translate"

        menu = Menu(
            MenuItem(status_text, None, enabled=False),
            Menu.SEPARATOR,
            MenuItem("Grammar correction", self._toggle_grammar, checked=grammar_checked),
            MenuItem("Translate to English", self._toggle_translate, checked=translate_checked),
            MenuItem("Add word to dictionary…", self._add_word),
            MenuItem("Edit settings…", self._open_config),
            MenuItem("Open logs folder", self._open_logs),
            Menu.SEPARATOR,
            MenuItem("Quit", self._quit),
        )
        self._icon = pystray.Icon(
            "voicetype", _make_image(_COLORS["loading"]), "VoiceType", menu
        )

    # -- lifecycle -------------------------------------------------------
    def run_detached(self) -> None:
        self._build()
        try:
            self._icon.run_detached()
        except Exception as exc:  # noqa: BLE001 - some backends lack run_detached
            log.debug("run_detached unavailable (%s); running in a thread.", exc)
            import threading

            threading.Thread(target=self._icon.run, daemon=True).start()

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass

    def set_state(self, state: str) -> None:
        self.state = state
        if self._icon is None:
            return
        try:
            self._icon.icon = _make_image(_COLORS.get(state, _COLORS["idle"]))
            self._icon.title = f"VoiceType — {self._labels.get(state, state)}"
            self._icon.update_menu()
        except Exception:  # noqa: BLE001 - tray updates are cosmetic
            pass

    # -- menu actions ----------------------------------------------------
    def _toggle_grammar(self, _icon=None, _item=None) -> None:
        self.app.toggle_grammar()
        self.set_state(self.state)

    def _toggle_translate(self, _icon=None, _item=None) -> None:
        self.app.toggle_translate()
        self.set_state(self.state)

    def _add_word(self, _icon=None, _item=None) -> None:
        import threading
        threading.Thread(target=self.app.add_dictionary_word, daemon=True,
                         name="add-word").start()

    def _open_config(self, _icon=None, _item=None) -> None:
        self._open(CONFIG_PATH)

    def _open_logs(self, _icon=None, _item=None) -> None:
        self._open(LOG_DIR)

    def _quit(self, _icon=None, _item=None) -> None:
        self.app.shutdown()

    @staticmethod
    def _open(path: Path) -> None:
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]  # Windows only
        except Exception:  # noqa: BLE001
            try:
                subprocess.Popen(["explorer", str(path)])
            except Exception:  # noqa: BLE001
                log.warning("Could not open %s", path)
