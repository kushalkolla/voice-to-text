"""Optional grammar/spelling correction via a local LanguageTool server.

LanguageTool is free and open-source and runs entirely on your machine using
the Java runtime — no cloud, no API key. The server starts lazily on first use
and is reused for every correction. If Java is missing or the engine can't
start, grammar simply switches off and dictation keeps working.
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)


class GrammarCorrector:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg["grammar"]
        self.enabled = bool(self.cfg.get("enabled", True))
        self._tool = None
        self._lock = threading.Lock()
        self._tried = False
        self.available = False

    def _ensure_tool(self) -> bool:
        """Start the LanguageTool server once. Returns True if it's usable."""
        if self._tool is not None:
            return True
        if self._tried and not self.available:
            return False
        with self._lock:
            if self._tool is not None:
                return True
            self._tried = True
            try:
                import language_tool_python  # lazy: pulls in the engine

                lang = self.cfg.get("language", "en-US")
                log.info("Starting local LanguageTool (%s) - first run downloads it...", lang)
                tool = language_tool_python.LanguageTool(lang)

                disabled_cats = self.cfg.get("disabled_categories") or []
                disabled_rules = self.cfg.get("disabled_rules") or []
                if disabled_cats:
                    try:
                        tool.disabled_categories.update(disabled_cats)
                    except Exception:  # noqa: BLE001 - older API shape
                        pass
                if disabled_rules:
                    try:
                        tool.disabled_rules.update(disabled_rules)
                    except Exception:  # noqa: BLE001
                        pass
                if self.cfg.get("picky"):
                    try:
                        tool.level = "picky"
                    except Exception:  # noqa: BLE001
                        pass

                self._tool = tool
                self.available = True
                log.info("LanguageTool ready.")
                return True
            except Exception as exc:  # noqa: BLE001 - Java missing, download blocked, etc.
                self.available = False
                log.warning(
                    "Grammar correction unavailable (%s). Dictation continues without it. "
                    "Install a Java runtime to enable it (winget install Microsoft.OpenJDK.17).",
                    exc,
                )
                return False

    def warmup_async(self) -> None:
        if self.enabled:
            threading.Thread(target=self._warmup, daemon=True,
                             name="languagetool-warmup").start()

    def _warmup(self) -> None:
        """Start the server *and* pay the engine's one-time JIT cost on a throwaway
        sentence in the background, so the first *real* correction is ~30 ms rather
        than the ~2.5 s cold-start penalty — which otherwise lands entirely on the
        user's very first dictated phrase and reads as "why is it so slow?"."""
        if not self._ensure_tool():
            return
        try:
            self._tool.correct("This is a warm up sentence.")
            log.info("Grammar engine warmed.")
        except Exception as exc:  # noqa: BLE001
            log.debug("grammar warmup correction failed: %s", exc)

    def correct(self, text: str) -> str:
        """Return a grammar-corrected copy of ``text`` (or the original on failure)."""
        if not self.enabled or not text.strip():
            return text
        if not self._ensure_tool():
            return text
        try:
            return self._tool.correct(text)
        except Exception as exc:  # noqa: BLE001 - never lose the user's words
            log.warning("Grammar pass failed (%s); using uncorrected text.", exc)
            return text

    def close(self) -> None:
        try:
            if self._tool is not None and hasattr(self._tool, "close"):
                self._tool.close()
        except Exception:  # noqa: BLE001
            pass
        self._tool = None
