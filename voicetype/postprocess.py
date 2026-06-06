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

# -- inverse text normalization (spoken -> written numbers & symbols) --------
_NUM_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_NUM_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_NUM_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000,
               "billion": 1_000_000_000}
_NUM_WORDS = set(_NUM_UNITS) | set(_NUM_TENS) | set(_NUM_SCALES)
# Longest-first so "seventeen" wins over "seven" in the alternation.
_NW = "|".join(sorted(_NUM_WORDS, key=len, reverse=True))
_NUM_RUN = r"(?:%s)(?:[\s-]+(?:and[\s-]+)?(?:%s))*" % (_NW, _NW)
_AMOUNT = r"(\d[\d,]*(?:\.\d+)?|%s)" % _NUM_RUN
_DOLLARS_RE = re.compile(
    r"\b%s\s+dollars?(?:\s+(?:and\s+)?%s\s+cents?)?" % (_AMOUNT, _AMOUNT), re.I)
_PERCENT_RE = re.compile(r"\b%s\s+percent\b" % _AMOUNT, re.I)
_DOMAIN_RE = re.compile(
    r"\s+dot\s+(com|org|net|io|edu|gov|co|app|dev|ai|me|us|uk)\b", re.I)


def _words_to_int(phrase: str):
    """Spoken whole number ("twenty five", "three thousand") -> int, or None if
    it isn't a clean whole number."""
    total = current = 0
    seen = False
    for t in re.split(r"[\s-]+", phrase.strip().lower()):
        if t in ("and", ""):
            continue
        if t in _NUM_UNITS:
            current += _NUM_UNITS[t]
        elif t in _NUM_TENS:
            current += _NUM_TENS[t]
        elif t == "hundred":
            current = (current or 1) * 100
        elif t in _NUM_SCALES:
            total += (current or 1) * _NUM_SCALES[t]
            current = 0
        else:
            return None
        seen = True
    return (total + current) if seen else None


def _amount_to_int(text: str):
    """An _AMOUNT capture (digits or words) -> int, or None if not a whole number."""
    s = (text or "").strip()
    if re.fullmatch(r"\d[\d,]*", s):
        return int(s.replace(",", ""))
    return _words_to_int(s)


def _normalize_numbers(text: str) -> str:
    """Spoken numbers/symbols -> written form: "five dollars" -> "$5",
    "twenty percent" -> "20%", "example dot com" -> "example.com".

    Conservative and idempotent: anything it can't parse cleanly is left exactly
    as-is, and digits Whisper already produced ("$5", "20%") aren't re-matched.
    """
    def _money(m):
        d = _amount_to_int(m.group(1))
        if d is None:
            return m.group(0)
        c = _amount_to_int(m.group(2)) if m.group(2) else None
        if c is not None and 0 <= c < 100:
            return "$%d.%02d" % (d, c)
        return "$%d" % d

    def _pct(m):
        s = m.group(1).strip()
        if re.fullmatch(r"\d[\d,]*(?:\.\d+)?", s):
            return s.replace(",", "") + "%"
        n = _words_to_int(s)
        return ("%d%%" % n) if n is not None else m.group(0)

    text = _DOLLARS_RE.sub(_money, text)
    text = _PERCENT_RE.sub(_pct, text)
    text = _DOMAIN_RE.sub(lambda m: "." + m.group(1).lower(), text)
    return text


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
        # Numbers/symbols last, so the spacing/capitalization passes above never
        # split an inserted "." (e.g. "example.com") back apart.
        if self.cfg.get("format_numbers", True):
            text = _normalize_numbers(text)
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
