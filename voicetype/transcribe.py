"""Local speech-to-text with faster-whisper.

Whisper produces well-punctuated, properly capitalized text out of the box,
which is the foundation of the "damn good punctuation" goal. The model loads
lazily (and can be warmed up in the background at startup) so the first
dictation is not slow.
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger(__name__)

_cuda_dlls_registered = False


def _enable_cuda_libs() -> None:
    """Put pip-installed CUDA libraries (cuBLAS/cuDNN) on the DLL search path.

    When the NVIDIA wheels are present their DLLs live under
    ``site-packages/nvidia/*/bin`` — a folder Windows does not search by default,
    so without this CTranslate2 silently fails to find them and falls back to CPU
    even on a perfectly good GPU. This makes one build work on every machine:
    GPUs light up automatically, and it's a harmless no-op on CPU-only PCs (no
    wheels to find) and on non-Windows. Runs once.
    """
    global _cuda_dlls_registered
    if _cuda_dlls_registered:
        return
    _cuda_dlls_registered = True
    if not hasattr(os, "add_dll_directory"):  # not Windows
        return
    try:
        import nvidia  # namespace package provided by the CUDA wheels
    except Exception:  # noqa: BLE001 - CPU-only install; nothing to register
        return
    dirs = []
    for base in list(getattr(nvidia, "__path__", [])):
        for sub in ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime"):
            d = os.path.join(base, sub, "bin")
            if os.path.isdir(d):
                dirs.append(d)
    for d in dirs:
        try:
            os.add_dll_directory(d)
        except Exception:  # noqa: BLE001
            pass
    # cuDNN 9 loads its own sub-DLLs (cudnn_ops/cudnn_engines…) with a plain
    # LoadLibrary that ignores add_dll_directory, so they must also be on PATH.
    if dirs:
        os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])


def _auto_device() -> str:
    """Prefer CUDA if a usable GPU is present, else CPU."""
    _enable_cuda_libs()
    try:
        from ctranslate2 import get_cuda_device_count

        if get_cuda_device_count() > 0:
            return "cuda"
    except Exception:  # noqa: BLE001
        pass
    return "cpu"


def _auto_compute_type(device: str) -> str:
    # int8 is fast and accurate on CPU; float16 is the GPU sweet spot.
    return "float16" if device == "cuda" else "int8"


def _cpu_threads(mcfg: dict) -> int:
    """How many CPU threads Whisper may use.

    Left at the library default, CTranslate2 grabs *every* core while decoding.
    On a CPU-only machine that starves the global keyboard hook for a moment and
    the UI stutters (and, if you use Win+H, that's exactly when Windows' own
    panel leaks through). Leaving a couple of cores free keeps the app responsive
    while it types. ``0`` (the default) means "auto: total cores minus two".
    """
    import os

    n = int(mcfg.get("cpu_threads", 0) or 0)
    if n > 0:
        return n
    return max(1, (os.cpu_count() or 4) - 2)


def _has_signal(audio, rms_floor: float = 0.0015) -> bool:
    """True if a clip carries enough energy to be worth transcribing without the
    VAD safety net. Guards the no-VAD retry from hallucinating words out of a
    near-silent clip, while still letting genuinely quiet speech through."""
    import numpy as np

    try:
        a = np.asarray(audio, dtype="float32")
        if a.size == 0:
            return False
        return float(np.sqrt(np.mean(a * a))) >= rms_floor
    except Exception:  # noqa: BLE001
        return True


def _normalize(audio, target: float = 0.9, max_gain: float = 40.0,
               min_level: float = 1.0e-4):
    """Scale a quiet clip up so Whisper and its VAD see a normal level.

    Uses a *robust* reference level (99.9th percentile of |sample|) rather than
    the raw maximum: a single full-scale click — common in shared-mode WASAPI
    streams — would otherwise read as "already loud" and defeat the boost while
    the real speech stays too faint for the VAD. We clip those rare spikes, then
    amplify so the bulk of the speech reaches ~0.9. A genuinely silent (muted)
    clip is left untouched so its hiss is not amplified into hallucinations.
    """
    import numpy as np

    try:
        a = np.asarray(audio, dtype="float32")
        if a.size == 0:
            return audio
        ref = float(np.quantile(np.abs(a), 0.999))
        if ref < min_level:
            return a  # essentially silence — let the guards drop it
        a = np.clip(a, -ref, ref)             # remove rare spikes that fool the level
        gain = min(max_gain, target / ref)
        if gain <= 1.0:
            return a.astype("float32")        # already loud enough; never attenuate
        log.info("Boosting quiet audio %.1fx (ref %.4f -> %.2f).",
                 gain, ref, ref * gain)
        return (a * gain).astype("float32")
    except Exception:  # noqa: BLE001
        return audio


def _denoise(audio, sr: int = 16000):
    """Gentle noise reduction before recognition (off unless ``audio.denoise``).

    Removes sub-80 Hz rumble (room/handling, never speech) and *softly* attenuates
    frequency bins sitting well below the clip's own noise floor. Deliberately
    conservative: Whisper is noise-robust, and full spectral subtraction creates
    artefacts that *hurt* recognition, so nothing is ever zeroed (a -6 dB floor)
    — we just take the edge off steady background noise.
    """
    import numpy as np

    try:
        a = np.asarray(audio, dtype="float32").reshape(-1)
        if a.size < sr // 4:           # < 0.25 s: too short to estimate noise
            return audio
        spec = np.fft.rfft(a)
        freqs = np.fft.rfftfreq(a.size, 1.0 / sr)
        mag = np.abs(spec)
        spec[freqs < 80.0] = 0.0       # high-pass: kill low-frequency rumble
        noise = float(np.percentile(mag, 25))
        if noise > 0.0:
            thresh = 1.5 * noise
            gain = np.ones_like(mag)
            low = mag < thresh
            gain[low] = np.maximum(0.5, mag[low] / thresh)   # attenuate by ≤6 dB
            spec = spec * gain
        out = np.fft.irfft(spec, n=a.size).astype("float32")
        return out if np.all(np.isfinite(out)) else audio
    except Exception:  # noqa: BLE001
        return audio


def _resolve_model_name(name: str, device: str, task: str) -> str:
    """Map the special model name ``"auto"`` to one sized to the hardware.

    A CUDA GPU runs ``large-v3-turbo`` — a pruned-decoder large-v3 that keeps
    nearly all of large-v3's accuracy (multilingual; strong on accented English
    and non-English words/names like "Telugu") while decoding several times
    faster, so live dictation stays snappy. On CPU that's still too slow, so we
    keep the light ``small.en`` (or multilingual ``small`` for translate). An
    explicit name always wins.

    Note: needs ~2 GB of VRAM; a very small GPU falls back automatically (see
    _load_candidates) or can pin a lighter model. Pin ``large-v3`` for maximum
    accuracy at the cost of speed.
    """
    if (name or "").strip().lower() != "auto":
        return name
    if device == "cuda":
        return "large-v3-turbo"  # near large-v3 accuracy, several times faster
    return "small" if task == "translate" else "small.en"


def _build_hotwords(cfg: dict) -> str:
    """Bias recognition toward the user's own vocabulary (names, brands, jargon).

    Whisper infers unusual proper nouns from acoustics alone and often spells or
    cases them wrong; passing them as hotwords nudges the decoder toward the
    right tokens *at recognition time*, complementing ``postprocess.replacements``
    (which only fixes text after the fact). Sourced from the corrected forms in
    ``postprocess.replacements`` so it stays config-driven and free.
    """
    pp = cfg.get("postprocess", {}) or {}
    repl = pp.get("replacements", {}) or {}
    seen, uniq = set(), []
    for v in repl.values():
        if isinstance(v, str) and v.strip() and v.lower() not in seen:
            seen.add(v.lower())
            uniq.append(v.strip())
    return " ".join(uniq)


_hotwords_supported = None


def _hotwords_ok() -> bool:
    """Whether the installed faster-whisper accepts a ``hotwords`` argument."""
    global _hotwords_supported
    if _hotwords_supported is None:
        try:
            import inspect
            from faster_whisper import WhisperModel

            _hotwords_supported = "hotwords" in inspect.signature(
                WhisperModel.transcribe).parameters
        except Exception:  # noqa: BLE001
            _hotwords_supported = False
    return _hotwords_supported


class Transcriber:
    """Wraps a faster-whisper model with lazy, thread-safe loading."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self._model = None
        self._load_lock = threading.Lock()
        self.device = ""
        self.compute_type = ""
        self.model_name = ""  # the concrete checkpoint actually loaded (resolves "auto")
        # "transcribe" (write what you said) or "translate" (any language -> English).
        self.task = str(cfg["model"].get("task", "transcribe") or "transcribe")
        self._warned_translate = False
        self.hotwords = _build_hotwords(cfg)

    # -- loading ---------------------------------------------------------
    def ensure_loaded(self) -> None:
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            from faster_whisper import WhisperModel

            _enable_cuda_libs()  # harmless if there's no GPU/wheels to find
            mcfg = self.cfg["model"]
            device = mcfg.get("device", "auto")
            if device == "auto":
                device = _auto_device()
            compute = mcfg.get("compute_type", "auto")
            if compute == "auto":
                compute = _auto_compute_type(device)

            threads = _cpu_threads(mcfg)
            last_exc = None
            for dev, name, comp in self._load_candidates(mcfg, device, compute):
                try:
                    log.info("Loading Whisper model %r (device=%s, compute=%s, "
                             "cpu_threads=%d) - first run downloads it...",
                             name, dev, comp, threads)
                    self._model = WhisperModel(name, device=dev, compute_type=comp,
                                               cpu_threads=threads)
                    # A GPU can load yet be unable to run (missing cuBLAS/cuDNN, or
                    # out of memory for a big model). Verify with a tiny inference
                    # before committing so we can step down to a smaller option.
                    if dev == "cuda" and not self._gpu_works():
                        raise RuntimeError("GPU loaded but the self-check inference "
                                           "failed (CUDA libraries or VRAM)")
                    self.device, self.compute_type, self.model_name = dev, comp, name
                    log.info("Model ready (model=%s, device=%s, compute=%s).",
                             name, dev, comp)
                    return
                except Exception as exc:  # noqa: BLE001 - try the next fallback
                    last_exc = exc
                    self._model = None
                    log.warning("Could not use %r on %s/%s (%s); trying the next "
                                "option.", name, dev, comp, exc)
            raise RuntimeError(f"Could not load any Whisper model ({last_exc})")

    def _load_candidates(self, mcfg, device, compute):
        """Ordered ``(device, model, compute)`` attempts.

        For ``model.name = "auto"`` on a GPU we try the best model (large-v3) and,
        if it won't load or run (e.g. a small-VRAM GPU), step down to a lighter
        one, then finally CPU — so every machine gets the best model it can
        actually run. An explicit model name is honoured as given, with only a CPU
        last resort. Consecutive duplicates are removed.
        """
        is_auto = (str(mcfg.get("name", "")).strip().lower() == "auto")
        primary = _resolve_model_name(mcfg["name"], device, self.task)
        cands = [(device, primary, compute)]
        if is_auto and device == "cuda":
            cands.append(("cuda", "medium", compute))   # lighter multilingual GPU model
        cpu_name = _resolve_model_name(mcfg["name"], "cpu", self.task)
        cands.append(("cpu", cpu_name, "int8"))         # always-works last resort
        out = []
        for c in cands:
            if not out or out[-1] != c:
                out.append(c)
        return out

    def _gpu_works(self) -> bool:
        """Run a throwaway inference to confirm the GPU can actually execute."""
        import numpy as np

        try:
            segments, _ = self._model.transcribe(
                np.zeros(16_000, dtype=np.float32), beam_size=1, vad_filter=False
            )
            list(segments)  # force the encoder to actually run
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("GPU self-check failed: %s", exc)
            return False

    def warmup_async(self) -> None:
        """Load the model in a background thread so startup feels instant."""
        threading.Thread(target=self._warmup, daemon=True, name="whisper-warmup").start()

    def _warmup(self) -> None:
        try:
            self.ensure_loaded()
        except Exception as exc:  # noqa: BLE001
            log.error("Background model warmup failed: %s", exc)

    def _reload_cpu(self) -> None:
        """Swap to a CPU model after a GPU failure so dictation keeps working."""
        from faster_whisper import WhisperModel

        mcfg = self.cfg["model"]
        name = _resolve_model_name(mcfg["name"], "cpu", self.task)
        self._model = WhisperModel(name, device="cpu", compute_type="int8",
                                   cpu_threads=_cpu_threads(mcfg))
        self.device, self.compute_type = "cpu", "int8"
        self.model_name = name
        log.info("Switched to CPU model %r (device=cpu, compute=int8).", name)

    # -- inference -------------------------------------------------------
    def transcribe(self, audio) -> str:
        """Return the transcript for a 16 kHz mono float32 numpy array."""
        self.ensure_loaded()
        # Optional gentle denoise (off by default) for consistently noisy rooms.
        if self.cfg.get("audio", {}).get("denoise", False):
            audio = _denoise(audio)
        # Quiet mics are the #1 cause of dropped dictation: the VAD deletes faint
        # speech and the model under-performs. Normalise the level first so both
        # behave as if the mic were loud — then the VAD pass usually succeeds and,
        # if it still over-trims, _has_signal reliably triggers the no-VAD retry.
        audio = _normalize(audio)
        vad_on = bool(self.cfg["model"].get("vad_filter", True))
        text = self._safe_run(audio, vad_on)
        # The built-in VAD sometimes discards an entire quiet / low-SNR phrase as
        # "non-speech". Our energy gate already isolated real speech upstream, so
        # an empty result here usually means VAD over-trimmed — retry once without
        # it (only if the clip actually carries signal) so we never silently drop
        # the user's words. This was the cause of "it captures but types nothing".
        if not text and vad_on and _has_signal(audio):
            log.info("Transcript empty after VAD; retrying without the VAD filter.")
            text = self._safe_run(audio, vad_filter=False)
        return text

    def _safe_run(self, audio, vad_filter: bool) -> str:
        """Run inference, transparently falling back GPU -> CPU on failure (a GPU
        that loads but can't execute, e.g. missing cuDNN, must not break every
        utterance)."""
        try:
            return self._run(audio, vad_filter)
        except Exception as exc:  # noqa: BLE001
            if self.device == "cuda":
                log.warning("GPU inference failed (%s); retrying on CPU.", exc)
                self._reload_cpu()
                return self._run(audio, vad_filter)
            raise

    def _run(self, audio, vad_filter: bool) -> str:
        mcfg = self.cfg["model"]
        task = "translate" if self.task == "translate" else "transcribe"
        # For translate, let Whisper detect the spoken language (any -> English).
        language = None if task == "translate" else (mcfg.get("language") or None)
        if task == "translate" and str(self.model_name or "").endswith(".en") \
                and not self._warned_translate:
            self._warned_translate = True
            log.warning("Translate mode needs a multilingual model, but %r is "
                        "English-only. Set model.name to 'auto' (or 'small'/'medium') "
                        "in config.json to translate other languages.", self.model_name)
        # Start at the configured temperature and let Whisper fall back to higher
        # ones only when a segment looks like a hallucination (a repetition trips
        # the compression-ratio guard, gibberish trips the log-prob guard). This
        # is Whisper's own anti-hallucination ladder; a lone 0.0 switched it off.
        t0 = float(mcfg.get("temperature", 0.0) or 0.0)
        temps = tuple(round(t0 + 0.2 * i, 1)
                      for i in range(6) if t0 + 0.2 * i <= 1.0 + 1e-6) or (t0,)
        kwargs = dict(
            task=task,
            language=language,
            beam_size=int(mcfg.get("beam_size", 5)),
            vad_filter=vad_filter,
            temperature=temps,
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
            no_speech_threshold=0.6,
            initial_prompt=mcfg.get("initial_prompt") or None,
            condition_on_previous_text=False,  # avoids run-on hallucinations in dictation
        )
        # Nudge the decoder toward the user's own names/jargon (free, local).
        if self.hotwords and _hotwords_ok():
            kwargs["hotwords"] = self.hotwords
        segments, _info = self._model.transcribe(audio, **kwargs)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        # Whisper occasionally double-spaces between segments.
        return " ".join(text.split())
