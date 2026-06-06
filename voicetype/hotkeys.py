"""Global hotkeys built on the ``keyboard`` library.

The default trigger is **Left Ctrl + Left Alt** — an easy bottom-left combo. It
needs no suppression and doesn't fight the OS. (It does share keys with every
``Ctrl+Alt+…`` shortcut, so those can toggle it too; a conflict-free key such as
``caps lock`` or ``right ctrl+right shift`` avoids that.) Press once to start,
again to stop.

Suppression is reserved for one case: if you set the trigger to **Win+H** (the
key Windows uses for its own Voice Typing), we register it with ``suppress=True``
so the press is swallowed before Windows sees it and our engine handles it
instead. For every other key suppression is forced off, because eating an
ordinary key would make it vanish from the focused app.

Why ``keyboard`` and not ``pynput``: only a low-level hook that can *suppress*
keys could take over Win+H. ``pynput`` can observe a combo but not stop Windows.

Note: a global low-level hook can't intercept keys while a window running as
Administrator is focused unless VoiceType is also elevated. For ordinary apps
(browser, Word, Notepad, chat) it works without admin.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

log = logging.getLogger(__name__)

# Map friendly names to what the ``keyboard`` library expects.
_ALIASES = {
    "win": "windows", "cmd": "windows", "super": "windows", "meta": "windows",
    "control": "ctrl", "ctl": "ctrl",
    "ctrl_r": "right ctrl", "rctrl": "right ctrl", "right control": "right ctrl",
    "ctrl_l": "left ctrl", "lctrl": "left ctrl", "left control": "left ctrl",
    "alt_r": "right alt", "ralt": "right alt", "alt_gr": "right alt", "altgr": "right alt",
    "alt_l": "left alt", "lalt": "left alt",
    "shift_r": "right shift", "rshift": "right shift",
    "shift_l": "left shift", "lshift": "left shift",
    "escape": "esc", "return": "enter", "spacebar": "space",
}


def _normalize(spec: str) -> str:
    """'win+h' / 'Win + H' -> 'windows+h' (keyboard-library syntax)."""
    spec = (spec or "").strip().lower()
    if not spec:
        return ""
    parts = [p.strip() for p in spec.split("+") if p.strip()]
    return "+".join(_ALIASES.get(p, p) for p in parts)


class HotkeyManager:
    def __init__(
        self,
        cfg: dict,
        on_toggle: Callable[[], None],
        on_ptt_press: Callable[[], None],
        on_ptt_release: Callable[[], None],
        on_cancel: Callable[[], None],
    ) -> None:
        hk = cfg["hotkeys"]
        self.toggle_spec = _normalize(hk.get("toggle", "left ctrl+left alt"))
        self.cancel_spec = _normalize(hk.get("cancel", ""))
        self.ptt_spec = _normalize(hk.get("push_to_talk", ""))
        # Suppression is only needed — and only safe — to steal Win+H from
        # Windows. For an ordinary key (e.g. Left Ctrl + Left Alt) we must NOT
        # eat it, or it would vanish from every app. So force it off unless the
        # trigger actually uses the Windows key.
        self.suppress_toggle = bool(hk.get("suppress_toggle", True)) and "windows" in self.toggle_spec
        # When the trigger uses the Windows key, swallowing the letter can leave
        # a lone Win press that pops the Start menu. Tapping a neutral key while
        # Win is held makes Windows treat it as a handled combo instead.
        self.tame_win = bool(hk.get("tame_win_key", True)) and "windows" in self.toggle_spec

        self.on_toggle = on_toggle
        self.on_ptt_press = on_ptt_press
        self.on_ptt_release = on_ptt_release
        self.on_cancel = on_cancel

        self._suspended = False
        self._ptt_down = False
        self._started = False
        self._tap_kb = None
        # Modifier-only combos (e.g. Left Ctrl + Left Alt) can fire several times
        # for one physical press; collapse fires closer than this into one toggle.
        self._toggle_debounce = max(0.0, float(hk.get("toggle_debounce_ms", 500)) / 1000.0)
        self._last_toggle = 0.0

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        import keyboard

        if self.toggle_spec:
            try:
                keyboard.add_hotkey(
                    self.toggle_spec, self._wrap(self._toggle_cb),
                    suppress=self.suppress_toggle, trigger_on_release=False,
                )
            except Exception as exc:  # noqa: BLE001 - retry without suppression
                log.warning("Could not register %s with suppression (%s); "
                            "registering without it - Windows' own panel may appear.",
                            self.toggle_spec, exc)
                keyboard.add_hotkey(self.toggle_spec, self._wrap(self._toggle_cb))

        if self.cancel_spec:
            # Don't suppress cancel — apps still need their Esc key.
            keyboard.add_hotkey(self.cancel_spec, self._wrap(self.on_cancel), suppress=False)

        if self.ptt_spec:
            if "+" in self.ptt_spec:
                log.warning("Push-to-talk supports a single key only; ignoring %r.", self.ptt_spec)
            else:
                keyboard.on_press_key(self.ptt_spec, self._ptt_press, suppress=False)
                keyboard.on_release_key(self.ptt_spec, self._ptt_release, suppress=False)

        self._started = True
        log.info(
            "Hotkeys active - toggle: %s | push-to-talk: %s | cancel: %s",
            self.toggle_spec or "(none)", self.ptt_spec or "(none)", self.cancel_spec or "(none)",
        )

    def stop(self) -> None:
        if not self._started:
            return
        try:
            import keyboard

            keyboard.unhook_all()
        except Exception:  # noqa: BLE001
            pass
        self._started = False

    def suspend(self) -> None:
        self._suspended = True

    def resume(self) -> None:
        self._suspended = False
        self._ptt_down = False  # clear any push-to-talk press that arrived while suspended

    # -- callbacks -------------------------------------------------------
    def _toggle_cb(self) -> None:
        # Debounce: a modifier-only combo can fire repeatedly for one press (and
        # autorepeats while held), which would start dictation and instantly stop
        # it. Ignore fires inside the debounce window, but keep pushing the window
        # forward so a held combo still counts as a single toggle.
        now = time.monotonic()
        if now - self._last_toggle < self._toggle_debounce:
            self._last_toggle = now
            return
        self._last_toggle = now
        # Fire while the Windows key is still held so the neutral tap counts as
        # part of the combo and the Start menu stays shut.
        if self.tame_win:
            self._tap_neutral()
        self.on_toggle()

    def _tap_neutral(self) -> None:
        """Tap Ctrl (injected) so a suppressed Win+H can't pop the Start menu."""
        try:
            if self._tap_kb is None:
                from pynput.keyboard import Controller

                self._tap_kb = Controller()
            from pynput.keyboard import Key

            self._tap_kb.press(Key.ctrl)
            self._tap_kb.release(Key.ctrl)
        except Exception as exc:  # noqa: BLE001 - cosmetic; never break the hotkey
            log.debug("tame-win tap failed: %s", exc)

    def _wrap(self, cb: Callable[[], None]):
        def inner(*_a, **_k):
            if self._suspended:
                return
            try:
                cb()
            except Exception as exc:  # noqa: BLE001 - keep the hook alive
                log.exception("Hotkey callback error: %s", exc)
        return inner

    def _ptt_press(self, _event) -> None:
        if self._suspended or self._ptt_down:
            return
        self._ptt_down = True
        self._wrap(self.on_ptt_press)()

    def _ptt_release(self, _event) -> None:
        if not self._ptt_down:
            return
        self._ptt_down = False
        self._wrap(self.on_ptt_release)()
