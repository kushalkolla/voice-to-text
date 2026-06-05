"""Insert finished text into whatever application is focused.

Two strategies:

* ``paste`` (default) — put the text on the clipboard and send Ctrl+V. Fast,
  Unicode-safe, and works almost everywhere. The previous clipboard is restored
  afterwards so we don't clobber what the user had copied.
* ``type`` — synthesize individual keystrokes. Slower, but works in the rare
  app that blocks paste.
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)


class Injector:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg["injection"]
        self._kb = None
        self._lock = threading.Lock()
        self.last_len = 0  # chars of the most recent insertion (for "scratch that")

    def _keyboard(self):
        if self._kb is None:
            from pynput.keyboard import Controller

            self._kb = Controller()
        return self._kb

    def insert(self, text: str) -> None:
        if not text:
            return
        if self.cfg.get("append_space", True):
            text = text + " "
        with self._lock:
            method = self.cfg.get("method", "paste")
            if method == "type":
                self._type(text)
            else:
                self._paste(text)
            self.last_len = len(text)

    # -- voice editing ---------------------------------------------------
    def backspace(self, n: int) -> None:
        """Delete the last ``n`` characters — backs out the previous insertion."""
        if n <= 0:
            return
        from pynput.keyboard import Key

        kb = self._keyboard()
        with self._lock:
            for _ in range(int(n)):
                kb.press(Key.backspace)
                kb.release(Key.backspace)
                if self.cfg.get("key_delay_ms", 2):
                    time.sleep(self.cfg.get("key_delay_ms", 2) / 1000.0)
            self.last_len = 0

    def undo(self) -> None:
        """Send Ctrl+Z to the focused app (the app's own undo)."""
        from pynput.keyboard import Key

        kb = self._keyboard()
        with self._lock:
            with kb.pressed(Key.ctrl):
                kb.press("z")
                kb.release("z")
            self.last_len = 0

    # -- paste -----------------------------------------------------------
    def _paste(self, text: str) -> None:
        import pyperclip
        from pynput.keyboard import Controller, Key

        previous = None
        restore = self.cfg.get("restore_clipboard", True)
        if restore:
            try:
                previous = pyperclip.paste()
            except Exception:  # noqa: BLE001
                previous = None

        try:
            pyperclip.copy(text)
        except Exception as exc:  # noqa: BLE001 - fall back to typing
            log.warning("Clipboard copy failed (%s); typing instead.", exc)
            self._type(text)
            return

        kb: Controller = self._keyboard()
        time.sleep(self.cfg.get("paste_delay_ms", 80) / 1000.0)
        with kb.pressed(Key.ctrl):
            kb.press("v")
            kb.release("v")

        if restore:
            # Restore after the target app has had a moment to read the clipboard.
            def _restore() -> None:
                time.sleep(0.4)
                try:
                    pyperclip.copy(previous if previous is not None else "")
                except Exception:  # noqa: BLE001
                    pass

            threading.Thread(target=_restore, daemon=True).start()

    # -- type ------------------------------------------------------------
    def _type(self, text: str) -> None:
        kb = self._keyboard()
        delay = self.cfg.get("key_delay_ms", 2) / 1000.0
        for ch in text:
            kb.type(ch)
            if delay:
                time.sleep(delay)
