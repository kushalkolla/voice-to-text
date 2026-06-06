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


class Transcriber:
    """Wraps a faster-whisper model with lazy, thread-safe loading."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self._model = None
        self._load_lock = threading.Lock()
        self.device = ""
        self.compute_type = ""
        # "transcribe" (write what you said) or "translate" (any language -> English).
        self.task = str(cfg["model"].get("task", "transcribe") or "transcribe")
        self._warned_translate = False

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
            log.info(
                "Loading Whisper model %r (device=%s, compute=%s, cpu_threads=%d) - first run downloads it...",
                mcfg["name"], device, compute, threads,
            )
            try:
                self._model = WhisperModel(mcfg["name"], device=device,
                                           compute_type=compute, cpu_threads=threads)
            except Exception as exc:  # noqa: BLE001 - retry on CPU/int8 if GPU path fails
                log.warning("Model load failed on %s/%s (%s); retrying on cpu/int8.",
                            device, compute, exc)
                device, compute = "cpu", "int8"
                self._model = WhisperModel(mcfg["name"], device=device,
                                           compute_type=compute, cpu_threads=threads)
            self.device, self.compute_type = device, compute

            # A GPU can load yet be unable to run (missing cuBLAS/cuDNN). Verify
            # with a tiny inference now so the first real dictation never fails.
            if self.device == "cuda" and not self._gpu_works():
                log.warning("GPU detected but its CUDA libraries are unusable "
                            "(e.g. cuBLAS/cuDNN missing); using CPU instead.")
                self._reload_cpu()
            log.info("Model ready (device=%s, compute=%s).", self.device, self.compute_type)

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
        self._model = WhisperModel(mcfg["name"], device="cpu", compute_type="int8",
                                   cpu_threads=_cpu_threads(mcfg))
        self.device, self.compute_type = "cpu", "int8"
        log.info("Switched to CPU model (device=cpu, compute=int8).")

    # -- inference -------------------------------------------------------
    def transcribe(self, audio) -> str:
        """Return the transcript for a 16 kHz mono float32 numpy array."""
        self.ensure_loaded()
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
        if task == "translate" and str(mcfg.get("name", "")).endswith(".en") \
                and not self._warned_translate:
            self._warned_translate = True
            log.warning("Translate mode needs a multilingual model, but %r is "
                        "English-only. Set model.name to 'small' (or 'base'/'medium') "
                        "in config.json to translate other languages.", mcfg.get("name"))
        segments, _info = self._model.transcribe(
            audio,
            task=task,
            language=language,
            beam_size=int(mcfg.get("beam_size", 5)),
            vad_filter=vad_filter,
            temperature=float(mcfg.get("temperature", 0.0)),
            initial_prompt=mcfg.get("initial_prompt") or None,
            condition_on_previous_text=False,  # avoids run-on hallucinations in dictation
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        # Whisper occasionally double-spaces between segments.
        return " ".join(text.split())
