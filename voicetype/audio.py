"""Microphone capture.

Records mono audio from the selected input device and returns it as a 16 kHz
float32 numpy array — exactly what Whisper wants. We record at the device's
native sample rate and resample with ``soxr`` so we never trip over a device
that refuses an exact 16 kHz stream.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

TARGET_SR = 16_000

log = logging.getLogger(__name__)


def list_input_devices() -> list[dict[str, Any]]:
    """Return available input devices as ``{index, name, channels, sr}`` dicts."""
    import sounddevice as sd  # lazy: heavy import

    out = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) > 0:
            out.append(
                {
                    "index": idx,
                    "name": dev["name"],
                    "channels": dev["max_input_channels"],
                    "sr": int(dev.get("default_samplerate", 0)),
                }
            )
    return out


def _resolve_device(setting: Any):
    """Map a config value (None / int / name substring) to a device index."""
    if setting is None or setting == "":
        return None
    if isinstance(setting, int):
        return setting
    # Treat as a case-insensitive name substring.
    needle = str(setting).lower()
    for dev in list_input_devices():
        if needle in dev["name"].lower():
            return dev["index"]
    log.warning("Input device %r not found; using system default.", setting)
    return None


class Recorder:
    """Start/stop microphone capture and hand back a 16 kHz mono waveform."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self._frames: list = []
        self._stream = None
        self._lock = threading.Lock()
        self._device_sr = TARGET_SR
        self.recording = False

    def start(self) -> bool:
        """Begin capturing. Returns False if the mic could not be opened."""
        import sounddevice as sd

        with self._lock:
            if self.recording:
                return True
            self._frames = []
            device = _resolve_device(self.cfg["audio"].get("input_device"))
            try:
                info = sd.query_devices(device, "input") if device is not None \
                    else sd.query_devices(kind="input")
                self._device_sr = int(info.get("default_samplerate") or TARGET_SR)
            except Exception:  # noqa: BLE001
                self._device_sr = TARGET_SR

            def _cb(indata, _frames, _time, status):  # PortAudio callback thread
                if status:
                    log.debug("audio status: %s", status)
                self._frames.append(indata.copy())

            try:
                self._stream = sd.InputStream(
                    samplerate=self._device_sr,
                    channels=1,
                    dtype="float32",
                    device=device,
                    callback=_cb,
                )
                self._stream.start()
            except Exception as exc:  # noqa: BLE001
                log.error("Could not open microphone: %s", exc)
                self._stream = None
                return False

            self.recording = True
            log.debug("Recording at %d Hz (device=%s)", self._device_sr, device)
            return True

    def stop(self):
        """Stop capturing and return a float32 16 kHz mono numpy array (or None)."""
        import numpy as np

        with self._lock:
            if not self.recording:
                return None
            self.recording = False
            try:
                if self._stream is not None:
                    self._stream.stop()
                    self._stream.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("Error closing stream: %s", exc)
            finally:
                self._stream = None

            if not self._frames:
                return None
            audio = np.concatenate(self._frames, axis=0).reshape(-1).astype("float32")
            self._frames = []

        if self._device_sr != TARGET_SR and audio.size:
            audio = self._resample(audio, self._device_sr, TARGET_SR)
        return audio

    @staticmethod
    def _resample(audio, src_sr: int, dst_sr: int):
        import numpy as np

        try:
            import soxr

            return soxr.resample(audio, src_sr, dst_sr).astype("float32")
        except Exception as exc:  # noqa: BLE001 - fall back to linear interpolation
            log.debug("soxr unavailable (%s); using linear resample.", exc)
            n_out = int(round(audio.size * dst_sr / src_sr))
            if n_out <= 0:
                return audio
            x_old = np.linspace(0.0, 1.0, num=audio.size, endpoint=False)
            x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
            return np.interp(x_new, x_old, audio).astype("float32")

    @staticmethod
    def duration_seconds(audio) -> float:
        return 0.0 if audio is None else float(len(audio)) / TARGET_SR


class StreamingRecorder:
    """Capture the mic continuously and emit one chunk per spoken phrase.

    A lightweight energy gate watches the incoming ~30 ms blocks. When speech is
    followed by a short pause (``silence_ms``) the phrase so far is handed to the
    ``on_segment`` callback as ``(float32 mono array, sample_rate)`` — still at
    the device's rate, because the heavy work (resampling + Whisper) must never
    run inside PortAudio's callback. This is what makes the app type live, phrase
    by phrase, instead of waiting for you to press the hotkey a second time.
    """

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self._stream = None
        self._lock = threading.Lock()
        self._device_sr = TARGET_SR
        self._block_ms = 30.0
        self.recording = False
        self._on_segment = None

    # -- lifecycle -------------------------------------------------------
    def start(self, on_segment) -> bool:
        """Begin streaming. Returns False if the mic could not be opened."""
        import sounddevice as sd

        with self._lock:
            if self.recording:
                return True
            self._on_segment = on_segment
            self._configure()
            device = _resolve_device(self.cfg["audio"].get("input_device"))
            try:
                info = sd.query_devices(device, "input") if device is not None \
                    else sd.query_devices(kind="input")
                self._device_sr = int(info.get("default_samplerate") or TARGET_SR)
            except Exception:  # noqa: BLE001
                self._device_sr = TARGET_SR

            blocksize = max(160, int(self._device_sr * 0.03))  # ~30 ms frames
            self._block_ms = 1000.0 * blocksize / self._device_sr
            self._reset_segmenter()

            try:
                self._stream = sd.InputStream(
                    samplerate=self._device_sr,
                    channels=1,
                    dtype="float32",
                    device=device,
                    blocksize=blocksize,
                    callback=self._cb,
                )
                self._stream.start()
            except Exception as exc:  # noqa: BLE001
                log.error("Could not open microphone: %s", exc)
                self._stream = None
                return False

            self.recording = True
            log.debug("Streaming at %d Hz (device=%s, block=%.0f ms)",
                      self._device_sr, device, self._block_ms)
            return True

    def stop(self, flush: bool = True):
        """Stop streaming. If ``flush`` and a phrase is in progress, emit it."""
        with self._lock:
            if not self.recording:
                return
            self.recording = False
            try:
                if self._stream is not None:
                    self._stream.stop()
                    self._stream.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("Error closing stream: %s", exc)
            finally:
                self._stream = None
            # The stream has stopped, so no callback can race us here.
            if flush and self._in_speech:
                self._emit_segment()
            self._seg = []
            self._in_speech = False
            self._on_segment = None

    # -- segmenter -------------------------------------------------------
    def _configure(self) -> None:
        s = self.cfg.get("streaming", {})
        self._silence_ms = float(s.get("silence_ms", 650))
        self._min_phrase_ms = float(s.get("min_phrase_ms", 300))
        self._max_phrase_ms = float(s.get("max_phrase_ms", 14000))
        self._onset_ms = float(s.get("onset_ms", 150))
        self._start_pad_ms = float(s.get("start_pad_ms", 200))
        self._energy_floor = float(s.get("energy_floor", 0.003))
        self._energy_mult = float(s.get("energy_mult", 3.0))

    def _reset_segmenter(self) -> None:
        import collections

        self._in_speech = False
        self._seg: list = []
        self._speech_run_ms = 0.0
        self._silence_run_ms = 0.0
        self._noise = None
        pad_blocks = max(1, int(round(self._start_pad_ms / max(self._block_ms, 1e-6))))
        self._pre = collections.deque(maxlen=pad_blocks)

    def _cb(self, indata, _frames, _time, status):  # PortAudio callback thread
        import numpy as np

        if status:
            log.debug("audio status: %s", status)
        block = np.asarray(indata, dtype="float32").reshape(-1)
        try:
            self._process(block)
        except Exception as exc:  # noqa: BLE001 - never let the audio thread die
            log.debug("segmenter error: %s", exc)

    def _process(self, block) -> None:
        import numpy as np

        rms = float(np.sqrt(np.mean(block * block))) if block.size else 0.0
        # Track a slow noise floor so the speech threshold rides above the room.
        if self._noise is None:
            self._noise = rms
        elif rms < self._noise:
            self._noise = 0.9 * self._noise + 0.1 * rms     # follow quiet down fast
        else:
            self._noise = 0.995 * self._noise + 0.005 * rms  # creep up slowly
        threshold = max(self._energy_floor, self._noise * self._energy_mult)
        speaking = rms > threshold

        if not self._in_speech:
            self._pre.append(block)
            if speaking:
                self._speech_run_ms += self._block_ms
                if self._speech_run_ms >= self._onset_ms:
                    self._in_speech = True          # a phrase has begun
                    self._seg = list(self._pre)     # keep the pre-roll so onsets aren't clipped
                    self._pre.clear()
                    self._silence_run_ms = 0.0
            else:
                self._speech_run_ms = 0.0
            return

        # Mid-phrase: accumulate until a long-enough pause (or hard length cap).
        self._seg.append(block)
        if speaking:
            self._silence_run_ms = 0.0
        else:
            self._silence_run_ms += self._block_ms
        seg_ms = len(self._seg) * self._block_ms
        if self._silence_run_ms >= self._silence_ms or seg_ms >= self._max_phrase_ms:
            self._emit_segment()

    def _emit_segment(self) -> None:
        import numpy as np

        seg, self._seg = self._seg, []
        was_speech = self._in_speech
        self._in_speech = False
        self._speech_run_ms = 0.0
        self._silence_run_ms = 0.0
        if not was_speech or not seg:
            return
        if len(seg) * self._block_ms < self._min_phrase_ms:
            return  # too short to be a real phrase
        audio = np.concatenate(seg, axis=0).reshape(-1).astype("float32")
        cb = self._on_segment
        if cb is not None:
            cb(audio, self._device_sr)

    @staticmethod
    def to_target_sr(audio, src_sr: int):
        """Resample an emitted phrase to 16 kHz for Whisper."""
        if src_sr == TARGET_SR or audio is None or not len(audio):
            return audio
        return Recorder._resample(audio, src_sr, TARGET_SR)
