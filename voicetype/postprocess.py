"""Text cleanup — the layer that makes dictation read like writing.

Whisper already punctuates and capitalizes well; this layer adds the polish a
dictation tool needs: custom word replacements, filler removal ("um", "uh"),
spoken formatting commands ("new line"), tidy spacing around punctuation, and
smart capitalization.

It runs in two phases around the (optional) grammar pass:

    text = pp.pre_grammar(raw)      # replacements, fillers — clean words for LanguageTool
    text = grammar.correct(text)    # optional
    text = pp.post_grammar(text)    # symbols, spacing, capitalization — final polish
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Characters that should hug the word before them (no space in front).
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.!?;:%)\]}])")
# A sentence end (.?! optionally followed by a closing quote/bracket) + space + letter.
_SENTENCE_BOUNDARY = re.compile(r'([.!?]["\')\]]?)(\s+)([a-z])')
# Standalone lowercase "i" (and "i'm", "i've"...) -> "I".
_LONE_I = re.compile(r"(?<![A-Za-z'])i(?![A-Za-z])")
_I_CONTRACTION = re.compile(r"(?<![A-Za-z'])i('[A-Za-z]+)")


class PostProcessor:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg["postprocess"]
        self._compile()

    def _compile(self) -> None:
        c = self.cfg
        # Custom dictionary: whole-word, case-insensitive, longest first so
        # multi-word keys win over their prefixes.
        self._replacements = []
        for key, val in sorted(c.get("replacements", {}).items(), key=lambda kv: -len(kv[0])):
            pat = re.compile(r"\b" + re.escape(key) + r"\b", re.IGNORECASE)
            self._replacements.append((pat, val))

        # Filler words removed when they stand alone (never inside another word).
        fillers = [re.escape(f) for f in c.get("fillers", []) if f.strip()]
        self._filler_re = (
            re.compile(r"(?<![A-Za-z'])(?:" + "|".join(fillers) + r")(?![A-Za-z'])",
                       re.IGNORECASE)
            if fillers else None
        )

        # Voice commands: spoken phrase -> literal text. Allow flexible spacing
        # and swallow a trailing comma/period Whisper may attach.
        self._commands = []
        for phrase, repl in sorted(c.get("commands", {}).items(), key=lambda kv: -len(kv[0])):
            spaced = r"\s+".join(re.escape(w) for w in phrase.split())
            pat = re.compile(r"(?<![A-Za-z])" + spaced + r"(?![A-Za-z])[.,]?", re.IGNORECASE)
            self._commands.append((pat, repl))

    # -- phase 1: before grammar ----------------------------------------
    def pre_grammar(self, text: str) -> str:
        text = " ".join((text or "").split())
        if not text:
            return ""
        for pat, val in self._replacements:
            text = pat.sub(val, text)
        if self.cfg.get("remove_fillers", True) and self._filler_re is not None:
            text = self._filler_re.sub("", text)
            text = re.sub(r"\s{2,}", " ", text)
            text = re.sub(r"\s+([,.!?;:])", r"\1", text)
        return text.strip()

    # -- phase 2: after grammar -----------------------------------------
    def post_grammar(self, text: str) -> str:
        if not text:
            return ""
        if self.cfg.get("voice_commands", True):
            text = self._apply_commands(text)
        if self.cfg.get("fix_spacing", True):
            text = self._fix_spacing(text)
        if self.cfg.get("smart_capitalize", True):
            text = self._capitalize(text)
        if self.cfg.get("capitalize_i", True):
            text = _I_CONTRACTION.sub(lambda m: "I" + m.group(1), text)
            text = _LONE_I.sub("I", text)
        return self._tidy_lines(text)

    # -- helpers ---------------------------------------------------------
    def _apply_commands(self, text: str) -> str:
        for pat, repl in self._commands:
            text = pat.sub(lambda _m, r=repl: r, text)
        # Don't leave a stray space before an inserted newline/tab.
        text = re.sub(r"[ \t]+([\n\t])", r"\1", text)
        text = re.sub(r"([\n\t])[ \t]+", r"\1", text)
        return text

    def _fix_spacing(self, text: str) -> str:
        text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
        # Ensure a space *after* sentence/clause punctuation when a letter follows
        # (but never split decimals like 3.14 or thousands like 1,000).
        text = re.sub(r"([,.!?;:])(?=[A-Za-z])", r"\1 ", text)
        # Collapse runs of spaces/tabs but keep newlines intact.
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text

    def _capitalize(self, text: str) -> str:
        # First letter of the whole text.
        text = re.sub(r"^(\s*)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)
        # First letter after a sentence boundary.
        text = _SENTENCE_BOUNDARY.sub(
            lambda m: m.group(1) + m.group(2) + m.group(3).upper(), text
        )
        # First letter at the start of any new line.
        text = re.sub(
            r"(\n\s*)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text
        )
        return text

    @staticmethod
    def _tidy_lines(text: str) -> str:
        # Strip trailing spaces per line and collapse 3+ blank lines to one gap.
        text = "\n".join(line.rstrip() for line in text.split("\n"))
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
