"""The application core: wires the pieces into a small state machine.

Default (streaming) flow — press the hotkey **once** and just talk:

    loading → idle → recording → idle
                       │  ▲          (mic stays open the whole time; each time you
                       │  │           pause, that phrase is transcribed and typed
                       └──┘           live, then it keeps listening)

You stop with the ✕ button on the pill, Esc, or by pressing the shortcut again —
you never have to press it a second time just to make text appear.

If ``streaming.enabled`` is false it falls back to the classic batch flow
(press once to record, press again to transcribe-and-insert the whole thing),
which is also what push-to-talk uses.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time

from . import sounds
from .audio import Recorder, StreamingRecorder
from .grammar import GrammarCorrector
from .hotkeys import HotkeyManager
from .inject import Injector
from .postprocess import PostProcessor
from .transcribe import Transcriber
from .tray import Tray
from .overlay import Overlay

log = logging.getLogger(__name__)

IDLE, RECORDING, TRANSCRIBING, LOADING, ERROR = (
    "idle", "recording", "transcribing", "loading", "error",
)

# Sentinel pushed onto the phrase queue to tell the consumer the session is over.
_END = object()


class App:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.state = LOADING
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._max_timer: threading.Timer | None = None
        self._busy = threading.Lock()  # serialize the transcription pipeline

        self.streaming = bool(cfg.get("streaming", {}).get("enabled", True))

        self.recorder = Recorder(cfg)              # batch / push-to-talk
        self.stream_rec = StreamingRecorder(cfg)   # live dictation
        self.transcriber = Transcriber(cfg)
        self.postproc = PostProcessor(cfg)
        self.grammar = GrammarCorrector(cfg)
        self.injector = Injector(cfg)

        # Live-dictation plumbing.
        self._seg_q: "queue.Queue" = queue.Queue()
        self._consumer: threading.Thread | None = None
        self._last_preview = ""
        self._warned_silent = False  # silent-mic guard latched this session?

        self.tray = Tray(self) if cfg["ui"].get("tray_icon", True) else None
        self.overlay = (
            Overlay(on_stop=self._request_stop)
            if cfg["ui"].get("show_overlay", True) else None
        )

        self.hotkeys = HotkeyManager(
            cfg,
            on_toggle=self.toggle,
            on_ptt_press=self._begin_batch,
            on_ptt_release=self._end_batch,
            on_cancel=self.cancel,
        )

    # -- run / shutdown --------------------------------------------------
    def run(self) -> None:
        self.hotkeys.start()
        if self.tray:
            self.tray.run_detached()
        self._set_state(LOADING)
        threading.Thread(target=self._warmup, daemon=True, name="warmup").start()

        if self.overlay is not None:
            self.overlay.run()  # blocks the main thread with the Tk loop
            if not self._stop.is_set():
                # Overlay couldn't start (no Tk); fall back to a headless wait.
                self._stop.wait()
        else:
            self._stop.wait()
        self._final_cleanup()

    def _warmup(self) -> None:
        try:
            if self.cfg["general"].get("warmup_on_start", True):
                self.transcriber.ensure_loaded()
            self.grammar.warmup_async()
        except Exception as exc:  # noqa: BLE001
            log.error("Warmup error: %s", exc)
        finally:
            if self.state == LOADING:
                self._set_state(IDLE)
        log.info("VoiceType is ready.")

    def shutdown(self) -> None:
        log.info("Shutting down…")
        self._stop.set()
        try:
            self.hotkeys.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.stream_rec.recording:
                self.stream_rec.stop(flush=False)
            if self.recorder.recording:
                self.recorder.stop()
        except Exception:  # noqa: BLE001
            pass
        # Release the consumer if it is parked on the queue.
        try:
            self._seg_q.put(_END)
        except Exception:  # noqa: BLE001
            pass
        if self.tray:
            self.tray.stop()
        if self.overlay is not None:
            self.overlay.request_close()

    def _final_cleanup(self) -> None:
        self._cancel_timer()
        try:
            self.grammar.close()
        except Exception:  # noqa: BLE001
            pass

    # -- state ------------------------------------------------------------
    def _set_state(self, state: str, text: str | None = None) -> None:
        self.state = state
        if self.tray:
            self.tray.set_state(state)
        if self.overlay is not None:
            self.overlay.set_state(state, text)

    def _overlay(self, state: str, text: str | None) -> None:
        if self.overlay is not None:
            self.overlay.set_state(state, text)

    # -- top-level controls ----------------------------------------------
    def toggle(self) -> None:
        if self.state == IDLE:
            self._begin()
        elif self.state == RECORDING:
            self._end()
        # While transcribing/loading, ignore the toggle.

    def _begin(self) -> None:
        if self.streaming:
            self._begin_stream()
        else:
            self._begin_batch()

    def _end(self) -> None:
        if self.streaming:
            self._end_stream(flush=True)
        else:
            self._end_batch()

    def cancel(self) -> None:
        """Esc: stop dictating. Streaming keeps everything already spoken;
        batch discards the in-progress recording (its classic behaviour)."""
        if self.state != RECORDING:
            return
        if self.streaming:
            self._end_stream(flush=True)
        else:
            self._cancel_batch()

    def _request_stop(self) -> None:
        """Called from the overlay's ✕ button (Tk thread); never block it."""
        threading.Thread(target=self._end, daemon=True, name="stop-click").start()

    # -- streaming dictation ---------------------------------------------
    def _begin_stream(self) -> None:
        with self._state_lock:
            if self.state != IDLE:
                return
            self._last_preview = ""
            self._warned_silent = False
            self._seg_q = queue.Queue()
            self._consumer = threading.Thread(
                target=self._consume, daemon=True, name="dictate")
            self._consumer.start()
            if not self.stream_rec.start(self._on_segment):
                self._seg_q.put(_END)  # let the just-started consumer exit
                sounds.play_error(self.cfg)
                self._flash_error()
                return
            self._set_state(RECORDING)
        sounds.play_start(self.cfg)
        self._start_silence_watchdog()
        log.info("Live dictation started.")

    def _start_silence_watchdog(self) -> None:
        """Watch the live mic; if it delivers only digital silence for the grace
        period, surface a clear "No mic input" warning instead of typing nothing.

        A working mic reads room noise well above ``silence_warn_peak`` within a
        second, so this never false-alarms on someone who simply hasn't spoken
        yet — it only fires for a muted, blocked, or wrong-device mic. If audio
        later appears, the warning clears itself."""
        grace = float(self.cfg["audio"].get("silence_warn_seconds", 4.0))
        if grace <= 0:
            return

        def _watch() -> None:
            if self._stop.wait(grace):  # initial grace; bail on shutdown
                return
            while self.state == RECORDING and self.stream_rec.recording:
                heard = self.stream_rec.heard_audio()
                if not heard and not self._warned_silent:
                    self._warned_silent = True
                    log.warning(
                        "No microphone input detected (peak=%.6f). The mic may be "
                        "muted, in use by another app, or the wrong device is "
                        "selected (see --check-mic).", self.stream_rec.session_peak())
                    sounds.play_error(self.cfg)
                    if self.overlay is not None:
                        self.overlay.set_state("warn", "🔇 No mic input — muted or in use?")
                elif heard and self._warned_silent:
                    self._warned_silent = False  # signal arrived; recover quietly
                    if self.overlay is not None:
                        self.overlay.set_state(RECORDING, self._last_preview or None)
                if self._stop.wait(1.0):
                    return

        threading.Thread(target=_watch, daemon=True, name="silence-watch").start()

    def _on_segment(self, audio, sr) -> None:
        """PortAudio thread: hand the finished phrase off and return fast."""
        self._seg_q.put((audio, sr))

    def _consume(self) -> None:
        """Transcribe and type each phrase in order until the session ends."""
        while True:
            item = self._seg_q.get()
            if item is _END:
                break
            audio, sr = item
            with self._busy:
                try:
                    audio16 = StreamingRecorder.to_target_sr(audio, sr)
                    text = self._transcribe_clean(audio16)
                    if text:
                        self._inject(text)
                        self._last_preview = _preview(text)
                    # Show the words we just typed as live progress.
                    if self.state == RECORDING:
                        self._overlay(RECORDING, self._last_preview or None)
                except Exception as exc:  # noqa: BLE001 - one bad phrase must not end the session
                    log.exception("Live phrase error: %s", exc)
        if not self._stop.is_set():
            # Only fall back to idle if we're still the finishing session, so a
            # stale consumer can't clobber a freshly started one back to idle.
            with self._state_lock:
                if self.state == TRANSCRIBING:
                    self._set_state(IDLE)
        log.info("Live dictation stopped.")

    def _end_stream(self, flush: bool = True) -> None:
        with self._state_lock:
            if self.state != RECORDING:
                return
            self._set_state(TRANSCRIBING, "Finishing…")
        sounds.play_stop(self.cfg)
        # Stop the mic (emitting any final phrase), then signal end-of-queue.
        threading.Thread(target=self._finish_stream, args=(flush,),
                         daemon=True, name="finish").start()

    def _finish_stream(self, flush: bool) -> None:
        try:
            self.stream_rec.stop(flush=flush)
        except Exception as exc:  # noqa: BLE001
            log.debug("stream stop error: %s", exc)
        finally:
            self._seg_q.put(_END)  # consumer drains the rest, then goes idle

    # -- batch / push-to-talk --------------------------------------------
    def _begin_batch(self) -> None:
        with self._state_lock:
            if self.state != IDLE:
                return
            if not self.recorder.start():
                sounds.play_error(self.cfg)
                self._flash_error()
                return
            self._set_state(RECORDING)
        sounds.play_start(self.cfg)
        self._arm_max_timer()

    def _end_batch(self) -> None:
        with self._state_lock:
            if self.state != RECORDING:
                return
            self._cancel_timer()
            audio = self.recorder.stop()
            self._set_state(TRANSCRIBING)
        sounds.play_stop(self.cfg)
        threading.Thread(target=self._pipeline_batch, args=(audio,), daemon=True,
                         name="pipeline").start()

    def _cancel_batch(self) -> None:
        with self._state_lock:
            if self.state != RECORDING:
                return
            self._cancel_timer()
            self.recorder.stop()  # discard the audio
            self._set_state(IDLE)
        sounds.play_stop(self.cfg)
        log.info("Recording cancelled.")

    def _pipeline_batch(self, audio) -> None:
        with self._busy:
            try:
                if self._is_silent(audio):
                    log.warning("Recording was digital silence — mic muted, in use, "
                                "or wrong device (see --check-mic).")
                    sounds.play_error(self.cfg)
                    self._flash_warn("🔇 No mic input — muted or in use?")
                    return
                text = self._transcribe_clean(audio)
                if text:
                    self._inject(text)
                self._set_state(IDLE)
            except Exception as exc:  # noqa: BLE001 - keep the app alive on any failure
                log.exception("Pipeline error: %s", exc)
                sounds.play_error(self.cfg)
                self._flash_error()

    # -- shared text pipeline --------------------------------------------
    def _transcribe_clean(self, audio) -> str | None:
        """Whisper → filler/spacing cleanup → grammar → final cleanup."""
        dur = Recorder.duration_seconds(audio)
        if audio is None or dur < self.cfg["audio"].get("min_seconds", 0.25):
            log.debug("Utterance too short (%.2fs); ignoring.", dur)
            return None

        t0 = time.time()
        raw = self.transcriber.transcribe(audio)
        log.info("Transcribed %.1fs of audio in %.2fs.", dur, time.time() - t0)
        if not raw.strip():
            return None

        action = _edit_action(raw)
        if action:
            self._run_edit(action)
            return None

        text = self.postproc.pre_grammar(raw)
        if self.grammar.enabled:
            text = self.grammar.correct(text)
        text = self.postproc.post_grammar(text)
        return text if text.strip() else None

    def _inject(self, text: str) -> None:
        log.info("Inserting: %s", text.replace("\n", "\\n"))
        self.hotkeys.suspend()
        try:
            self.injector.insert(text)
        finally:
            self.hotkeys.resume()

    def _run_edit(self, action: str) -> None:
        """Carry out a spoken edit command instead of typing it."""
        self.hotkeys.suspend()
        try:
            if action == "undo":
                self.injector.undo()
                label = "↶ undo"
            else:  # delete / "scratch that"
                self.injector.backspace(self.injector.last_len)
                label = "⌫ scratched"
        finally:
            self.hotkeys.resume()
        log.info("Voice edit: %s", action)
        if self.state == RECORDING:
            self._overlay(RECORDING, label)

    # -- grammar toggle (tray) -------------------------------------------
    def toggle_grammar(self) -> None:
        self.grammar.enabled = not self.grammar.enabled
        log.info("Grammar correction %s.", "on" if self.grammar.enabled else "off")
        if self.grammar.enabled:
            self.grammar.warmup_async()

    # -- translate toggle (tray) -----------------------------------------
    def toggle_translate(self) -> None:
        self.transcriber.task = (
            "transcribe" if self.transcriber.task == "translate" else "translate"
        )
        log.info("Speech task: %s.", self.transcriber.task)

    # -- add word to dictionary (tray) -----------------------------------
    def add_dictionary_word(self) -> None:
        """Prompt for a word/name and add it so VoiceType spells/capitalizes it
        right and is biased to recognize it. Saves config and reloads live."""
        word = (_prompt_for_word(
            "VoiceType — add word",
            "Add a word or name so VoiceType spells and capitalizes it correctly\n"
            "(e.g. CourseGlance, Airceleo, Kushal):") or "").strip()
        if not word:
            return
        try:
            repl = self.cfg.setdefault("postprocess", {}).setdefault("replacements", {})
            repl[word.lower()] = word
            from .config import save_config
            save_config(self.cfg)
            # Reload the pieces that use the dictionary, live (no restart).
            self.postproc = PostProcessor(self.cfg)
            from .transcribe import _build_hotwords
            self.transcriber.hotwords = _build_hotwords(self.cfg)
            log.info("Added dictionary word: %s", word)
            if self.overlay is not None:
                self.overlay.set_state("warn", f"✓ Added “{word}”")
                t = threading.Timer(2.0, lambda: self._set_state(self.state))
                t.daemon = True
                t.start()
        except Exception as exc:  # noqa: BLE001
            log.error("Could not add dictionary word: %s", exc)

    # -- helpers ---------------------------------------------------------
    def _arm_max_timer(self) -> None:
        self._cancel_timer()
        secs = self.cfg["audio"].get("max_seconds", 120)
        self._max_timer = threading.Timer(secs, self._auto_stop)
        self._max_timer.daemon = True
        self._max_timer.start()

    def _auto_stop(self) -> None:
        if self.state == RECORDING:
            log.info("Reached max recording length; stopping.")
            self._end_batch()

    def _cancel_timer(self) -> None:
        if self._max_timer is not None:
            self._max_timer.cancel()
            self._max_timer = None

    def _flash_error(self) -> None:
        self._set_state(ERROR)
        t = threading.Timer(1.5, lambda: self._set_state(IDLE))
        t.daemon = True
        t.start()

    def _flash_warn(self, msg: str) -> None:
        """Show an amber 'no mic input' pill for a moment, then return to idle."""
        if self.overlay is not None:
            self.overlay.set_state("warn", msg)
        t = threading.Timer(2.8, lambda: self._set_state(IDLE))
        t.daemon = True
        t.start()

    def _is_silent(self, audio) -> bool:
        """True if a recorded clip is digital silence (muted/blocked mic) — judged
        by the fraction of nonzero samples, not loudness, so a quiet-but-working
        mic is never mistaken for a dead one. Requires ≥0.5 s to avoid misjudging
        a brief tap."""
        import numpy as np
        from .audio import SILENT_NONZERO_FRAC

        if audio is None or not len(audio):
            return False
        if Recorder.duration_seconds(audio) < 0.5:
            return False
        a = np.asarray(audio, dtype="float32")
        frac = (float(np.count_nonzero(a)) / a.size) if a.size else 0.0
        return frac < SILENT_NONZERO_FRAC


def _preview(text: str, width: int = 22) -> str:
    """A short, single-line tail of what we just typed, for the status pill."""
    flat = " ".join(text.split())
    if len(flat) <= width:
        return flat
    return "…" + flat[-(width - 1):]


def _prompt_for_word(title: str, prompt: str) -> str:
    """Show a one-field input dialog in a *separate* process and return the text.

    Out-of-process on purpose: the overlay already owns a Tk main loop on the
    main thread, and a second Tk root in the same process is unsupported."""
    import subprocess
    import sys

    script = (
        "import tkinter as tk\n"
        "from tkinter import simpledialog\n"
        "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
        "s = simpledialog.askstring(%r, %r, parent=r)\n"
        "import sys; sys.stdout.write(s or '')\n"
        "r.destroy()\n" % (title, prompt)
    )
    try:
        out = subprocess.run([sys.executable, "-c", script],
                             capture_output=True, text=True, timeout=120)
        return out.stdout or ""
    except Exception as exc:  # noqa: BLE001
        log.debug("word prompt failed: %s", exc)
        return ""


# Spoken edits: only fire when the *whole* utterance is the command, so normal
# speech ("I need to scratch that itch") is never mistaken for a command.
_EDIT_COMMANDS = {
    "scratch that": "delete", "delete that": "delete", "scratch": "delete",
    "undo that": "undo", "undo": "undo",
}


def _edit_action(raw: str) -> str | None:
    key = re.sub(r"[^a-z ]", "", (raw or "").lower())
    key = re.sub(r"\s+", " ", key).strip()
    return _EDIT_COMMANDS.get(key)
