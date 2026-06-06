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

# How we tell a working mic from a dead one — by the *fraction of nonzero
# samples*, not loudness. A live mic emits nonzero electrical dither even in a
# silent room (measured ~80%+ nonzero here); a muted mic, an app holding it
# exclusively, or the wrong/disconnected device delivers a stream of exact zeros
# (0% nonzero). This is far more reliable than a magnitude threshold, which a
# quiet room can dip below while the mic is perfectly fine. Below this fraction =
# treated as digital silence.
SILENT_NONZERO_FRAC = 0.02

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


def _wasapi_default_input():
    """Index of the WASAPI host API's default input device, or None.

    WASAPI is Windows' modern, shared-mode capture path. PortAudio's plain
    default points at the legacy MME emulation, which we measured as flaky when
    another app is also using the mic; the WASAPI device shares cleanly.
    """
    import sounddevice as sd

    try:
        for ha in sd.query_hostapis():
            if ha.get("name") == "Windows WASAPI":
                di = ha.get("default_input_device", -1)
                if di is not None and di >= 0:
                    return int(di)
    except Exception as exc:  # noqa: BLE001
        log.debug("WASAPI default lookup failed: %s", exc)
    return None


def _resolve_device(setting: Any, prefer_wasapi: bool = True):
    """Map a config value (None / int / name substring) to a device index.

    When no device is configured, prefer the WASAPI default input over the MME
    default so VoiceType captures through the shared-mode path (lets it coexist
    with another mic app, and is lower-latency). ``None`` still means "let
    PortAudio pick" if WASAPI can't be resolved."""
    if setting is None or setting == "":
        if prefer_wasapi:
            w = _wasapi_default_input()
            if w is not None:
                log.debug("Using WASAPI default input device (index %d).", w)
                return w
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


def probe_input(cfg: dict, seconds: float = 3.0) -> dict[str, Any]:
    """Record a short sample from the configured device and measure its level.

    Returns a dict describing what was captured so a caller can tell a working
    mic from a muted / blocked / wrong-device one. ``verdict`` is one of
    ``"ok"``, ``"quiet"``, ``"silent"`` (or ``"error"`` with ``error`` set).
    """
    import numpy as np
    import sounddevice as sd

    acfg = cfg.get("audio", {})
    device = _resolve_device(acfg.get("input_device"), acfg.get("prefer_wasapi", True))
    result: dict[str, Any] = {"requested": acfg.get("input_device"),
                              "device_index": device}
    try:
        info = (sd.query_devices(device, "input") if device is not None
                else sd.query_devices(kind="input"))
        result["name"] = info.get("name", "?")
        sr = int(info.get("default_samplerate") or TARGET_SR)
        result["samplerate"] = sr
        frames = max(1, int(seconds * sr))
        rec = sd.rec(frames, samplerate=sr, channels=1, dtype="float32", device=device)
        sd.wait()
    except Exception as exc:  # noqa: BLE001
        result["verdict"] = "error"
        result["error"] = str(exc)
        return result

    a = np.asarray(rec, dtype="float32").reshape(-1)
    peak = float(np.max(np.abs(a))) if a.size else 0.0
    rms = float(np.sqrt(np.mean(a * a))) if a.size else 0.0
    nonzero_frac = (float(np.count_nonzero(a)) / a.size) if a.size else 0.0
    result["samples"] = int(a.size)
    result["peak"] = peak
    result["rms"] = rms
    result["nonzero_frac"] = nonzero_frac
    if nonzero_frac < SILENT_NONZERO_FRAC:
        result["verdict"] = "silent"   # exact zeros: muted / in use / wrong device
    elif rms < 1e-3:
        result["verdict"] = "quiet"    # alive but faint
    else:
        result["verdict"] = "ok"
    return result


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
            acfg = self.cfg["audio"]
            device = _resolve_device(acfg.get("input_device"),
                                     acfg.get("prefer_wasapi", True))
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
        # Telemetry that drives the silent-mic guard, updated in the PortAudio
        # callback and read from other threads (scalar reads are atomic enough).
        # _session_nonzero/_session_samples give the nonzero fraction (alive vs
        # blocked); _session_peak/_cur_rms are for display and the level meter.
        self._session_peak = 0.0
        self._cur_rms = 0.0
        self._session_nonzero = 0
        self._session_samples = 0

    # -- lifecycle -------------------------------------------------------
    def start(self, on_segment) -> bool:
        """Begin streaming. Returns False if the mic could not be opened."""
        import sounddevice as sd

        with self._lock:
            if self.recording:
                return True
            self._on_segment = on_segment
            self._configure()
            acfg = self.cfg["audio"]
            device = _resolve_device(acfg.get("input_device"),
                                     acfg.get("prefer_wasapi", True))
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
        self._release_mult = float(s.get("release_mult", 0.6))

    def _reset_segmenter(self) -> None:
        import collections

        self._in_speech = False
        self._seg: list = []
        self._speech_run_ms = 0.0
        self._silence_run_ms = 0.0
        self._noise = None
        self._session_peak = 0.0
        self._cur_rms = 0.0
        self._session_nonzero = 0
        self._session_samples = 0
        pad_blocks = max(1, int(round(self._start_pad_ms / max(self._block_ms, 1e-6))))
        self._pre = collections.deque(maxlen=pad_blocks)

    def _cb(self, indata, _frames, _time, status):  # PortAudio callback thread
        import numpy as np

        if status:
            log.debug("audio status: %s", status)
        # COPY, don't view: ``indata`` is PortAudio's internal buffer and is
        # recycled on the very next callback. ``np.asarray(..., float32)`` does
        # *not* copy a float32 input, so storing that view hands the segmenter
        # memory that is overwritten/freed before the phrase is concatenated —
        # which surfaced as astronomical (~1e28) garbage samples and pure noise
        # to Whisper. ``np.array`` makes an owned copy, exactly like Recorder._cb.
        block = np.array(indata, dtype="float32").reshape(-1)
        try:
            self._process(block)
        except Exception as exc:  # noqa: BLE001 - never let the audio thread die
            log.debug("segmenter error: %s", exc)

    def _process(self, block) -> None:
        import numpy as np

        rms = float(np.sqrt(np.mean(block * block))) if block.size else 0.0
        # Cheap level telemetry for the silent-mic guard and (future) level meter.
        if block.size:
            peak = float(np.max(np.abs(block)))
            if peak > self._session_peak:
                self._session_peak = peak
            self._session_nonzero += int(np.count_nonzero(block))
            self._session_samples += block.size
        self._cur_rms = rms
        # Track a slow noise floor so the speech threshold rides above the room.
        if self._noise is None:
            self._noise = rms
        elif rms < self._noise:
            self._noise = 0.9 * self._noise + 0.1 * rms     # follow quiet down fast
        else:
            self._noise = 0.995 * self._noise + 0.005 * rms  # creep up slowly
        # Hysteresis: a higher bar to *start* a phrase, a lower bar to *continue*
        # one, so soft syllables and brief between-word dips don't get mistaken
        # for the end of a sentence (the #1 cause of phrases cut mid-thought).
        start_threshold = max(self._energy_floor, self._noise * self._energy_mult)
        release_threshold = start_threshold * self._release_mult

        if not self._in_speech:
            self._pre.append(block)
            if rms > start_threshold:
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
        if rms > release_threshold:
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

    # -- level telemetry -------------------------------------------------
    def session_peak(self) -> float:
        """Loudest sample (0..1) heard since the current session started."""
        return self._session_peak

    def current_rms(self) -> float:
        """RMS (0..1) of the most recent audio block — a live level reading."""
        return self._cur_rms

    def heard_audio(self) -> bool:
        """True once a live signal has arrived; False means digital silence
        (mic muted, held by another app, or the wrong input device). Judged by
        the fraction of nonzero samples — robust to a merely quiet room."""
        if self._session_samples <= 0:
            return False
        return (self._session_nonzero / self._session_samples) >= SILENT_NONZERO_FRAC

    @staticmethod
    def to_target_sr(audio, src_sr: int):
        """Resample an emitted phrase to 16 kHz for Whisper."""
        if src_sr == TARGET_SR or audio is None or not len(audio):
            return audio
        return Recorder._resample(audio, src_sr, TARGET_SR)
