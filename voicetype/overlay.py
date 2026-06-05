"""A small always-on-top status pill with a microphone icon.

While dictating it shows a familiar **mic icon** (red while listening), the most
recent words it just typed — so you can watch the progress — and a round ✕ stop
button you can click to end dictation.

Optional and fully guarded: if Tk is unavailable or anything goes wrong, the app
keeps working with just the tray icon and sounds (and Esc / the shortcut still
stop dictation). It runs its Tk main loop on the main thread; other threads talk to
it through a thread-safe queue, so we never touch Tk widgets off-thread.

It is deliberately a borderless, no-activate window so it never steals keyboard
focus from the text box you are dictating into — including when you click ✕.
The mic is drawn on a canvas (not a font glyph) so it always renders cleanly,
and it is positioned once per appearance so it never jitters as the text changes.
"""
from __future__ import annotations

import logging
import queue

log = logging.getLogger(__name__)

_VISUALS = {
    "loading": ("#ffb800", "Loading model…"),
    "idle": ("#569c68", "Ready"),
    "recording": ("#e0322f", "Listening…"),
    "transcribing": ("#3482e2", "Finishing…"),
    "error": ("#c81e1e", "Error"),
    "warn": ("#ffa01e", "No mic input"),
    "paused": ("#888888", "Paused"),
}
# States that should actually display the pill (idle stays hidden, unobtrusive).
_VISIBLE = {"loading", "recording", "transcribing", "error", "warn"}
# States where the ✕ stop button makes sense (an active dictation session, plus
# "warn" — that fires mid-dictation, so you still want a way to stop).
_STOPPABLE = {"recording", "transcribing", "warn"}


class Overlay:
    def __init__(self, on_close=None, on_stop=None) -> None:
        self._q: "queue.Queue[tuple[str, str | None]]" = queue.Queue()
        self._on_close = on_close
        self._on_stop = on_stop
        self._root = None
        self._mic = None
        self._label = None
        self._stop_btn = None
        self._available = True
        self._visible = False

    def set_state(self, state: str, text: str | None = None) -> None:
        self._q.put((state, text))

    def request_close(self) -> None:
        self._q.put(("__quit__", None))

    @property
    def available(self) -> bool:
        return self._available

    def run(self) -> None:
        """Create the window and run the Tk loop (blocks the calling thread)."""
        try:
            import tkinter as tk
        except Exception as exc:  # noqa: BLE001
            self._available = False
            log.warning("Overlay unavailable (no Tk: %s).", exc)
            return

        try:
            root = tk.Tk()
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            try:
                root.attributes("-alpha", 0.95)
            except Exception:  # noqa: BLE001
                pass
            root.configure(bg="#1c1c1e")

            frame = tk.Frame(root, bg="#1c1c1e", padx=13, pady=8)
            frame.pack()

            self._mic = tk.Canvas(frame, width=20, height=22, bg="#1c1c1e",
                                  highlightthickness=0)
            self._mic.pack(side="left", padx=(0, 9))
            self._draw_mic("#569c68")

            # Fixed width keeps the pill from resizing/jumping as the live text changes.
            self._label = tk.Label(frame, text="Ready", fg="#f2f2f2", bg="#1c1c1e",
                                   font=("Segoe UI", 11, "bold"), width=22, anchor="w")
            self._label.pack(side="left")

            # The ✕ stop button. A Label (not a Button) so it can't take keyboard
            # focus away from the text box you're dictating into.
            self._stop_btn = tk.Label(frame, text="✕", fg="#9a9a9a", bg="#2a2a2d",
                                      font=("Segoe UI", 11, "bold"), width=2,
                                      padx=2, pady=0, cursor="hand2")
            self._stop_btn.pack(side="left", padx=(10, 0))
            self._stop_btn.bind("<Button-1>", self._on_stop_click)
            self._stop_btn.bind("<Enter>",
                                 lambda _e: self._stop_btn.config(fg="#ffffff", bg="#d23b3b"))
            self._stop_btn.bind("<Leave>",
                                 lambda _e: self._stop_btn.config(fg="#9a9a9a", bg="#2a2a2d"))

            root.update_idletasks()
            self._make_no_activate(root)  # never steal focus from the target app
            root.withdraw()  # start hidden until something happens
            self._root = root
            root.after(60, self._poll)
            root.mainloop()
        except Exception as exc:  # noqa: BLE001
            self._available = False
            log.warning("Overlay failed to start (%s).", exc)

    # -- drawing ---------------------------------------------------------
    def _draw_mic(self, color: str) -> None:
        """Draw a simple, always-rendering microphone in the given colour."""
        c = self._mic
        try:
            c.delete("all")
            c.create_oval(6, 2, 14, 13, fill=color, outline=color)          # capsule head
            c.create_arc(3, 5, 17, 17, start=200, extent=140, style="arc",  # stand
                         outline=color, width=2)
            c.create_line(10, 16, 10, 19, fill=color, width=2)              # stem
            c.create_line(6, 19, 14, 19, fill=color, width=2)              # base
        except Exception:  # noqa: BLE001
            pass

    # -- internals -------------------------------------------------------
    def _on_stop_click(self, _event=None) -> None:
        if self._on_stop is None:
            return
        try:
            self._on_stop()
        except Exception as exc:  # noqa: BLE001
            log.debug("stop-button handler error: %s", exc)

    @staticmethod
    def _make_no_activate(root) -> None:
        """Apply WS_EX_NOACTIVATE so the pill never steals focus.

        Without this, popping the overlay up — or clicking the ✕ — could pull
        keyboard focus off the text box, and the next paste would land nowhere.
        Windows-only; any failure is harmless.
        """
        try:
            import ctypes

            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(root.winfo_id())
            if not hwnd:
                hwnd = root.winfo_id()
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
            )
        except Exception:  # noqa: BLE001 - non-Windows or restricted; pill still works
            pass

    def _position(self) -> None:
        root = self._root
        root.update_idletasks()
        w = root.winfo_width() or 220
        h = root.winfo_height() or 40
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        x = (sw - w) // 2
        y = sh - h - 90  # hover just above the taskbar
        root.geometry(f"+{x}+{y}")

    def _poll(self) -> None:
        try:
            while True:
                state, text = self._q.get_nowait()
                if state == "__quit__":
                    if self._on_close:
                        try:
                            self._on_close()
                        except Exception:  # noqa: BLE001
                            pass
                    self._root.destroy()
                    return
                self._apply(state, text)
        except queue.Empty:
            pass
        if self._root is not None:
            self._root.after(60, self._poll)

    def _apply(self, state: str, text: str | None) -> None:
        color, default_label = _VISUALS.get(state, _VISUALS["idle"])
        label = text if text else default_label
        try:
            self._draw_mic(color)
            self._label.config(text=label)
            if self._stop_btn is not None:
                if state in _STOPPABLE:
                    self._stop_btn.pack(side="left", padx=(10, 0))
                else:
                    self._stop_btn.pack_forget()

            should_show = state in _VISIBLE
            if should_show and not self._visible:
                # Re-centre only when it (re)appears, never on every text update,
                # so the pill stays rock-steady while you dictate.
                self._position()
                self._root.deiconify()
                self._root.lift()
                self._visible = True
            elif not should_show and self._visible:
                self._root.withdraw()
                self._visible = False
        except Exception:  # noqa: BLE001
            pass
