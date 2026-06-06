"""Unit tests for VoiceType's offline text pipeline.

Run from the project root:  python -m unittest discover -s tests -v
No third-party test framework needed (stdlib unittest), and these are not
bundled into the installer.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voicetype.postprocess import _normalize_numbers, _words_to_int, PostProcessor
from voicetype.transcribe import _resolve_model_name, _build_hotwords
from voicetype.config import DEFAULTS, _deep_merge


def _pp(**postprocess_overrides):
    cfg = _deep_merge(DEFAULTS, {"postprocess": postprocess_overrides})
    return PostProcessor(cfg)


class TestNumberFormatting(unittest.TestCase):
    def test_currency(self):
        self.assertEqual(_normalize_numbers("five dollars"), "$5")
        self.assertEqual(_normalize_numbers("five dollars and fifty cents"), "$5.50")
        self.assertEqual(_normalize_numbers("three dollars and five cents"), "$3.05")
        self.assertEqual(_normalize_numbers("one hundred and fifty dollars"), "$150")
        self.assertEqual(_normalize_numbers("it costs five thousand two hundred dollars"),
                         "it costs $5200")

    def test_percent(self):
        self.assertEqual(_normalize_numbers("twenty percent"), "20%")
        self.assertEqual(_normalize_numbers("twenty five percent"), "25%")

    def test_domain(self):
        self.assertEqual(_normalize_numbers("visit example dot com"), "visit example.com")

    def test_no_false_positives(self):
        self.assertEqual(_normalize_numbers("I have one idea"), "I have one idea")
        self.assertEqual(_normalize_numbers("connect the dots"), "connect the dots")

    def test_idempotent(self):
        self.assertEqual(_normalize_numbers("$5"), "$5")
        self.assertEqual(_normalize_numbers("20%"), "20%")
        self.assertEqual(_normalize_numbers(_normalize_numbers("five dollars")), "$5")

    def test_words_to_int(self):
        self.assertEqual(_words_to_int("twenty five"), 25)
        self.assertEqual(_words_to_int("one hundred and fifty"), 150)
        self.assertEqual(_words_to_int("three thousand"), 3000)
        self.assertIsNone(_words_to_int("banana"))


class TestPostProcessor(unittest.TestCase):
    def test_fillers_removed(self):
        self.assertEqual(_pp().pre_grammar("um I think uh yes"), "I think yes")

    def test_replacements_whole_word_case_insensitive(self):
        pp = _pp(replacements={"github": "GitHub"})
        self.assertEqual(pp.pre_grammar("push to github"), "push to GitHub")
        self.assertEqual(pp.pre_grammar("githubbed"), "githubbed")  # whole word only

    def test_capitalize_and_lone_i(self):
        self.assertEqual(_pp().post_grammar("i went home. then i slept"),
                         "I went home. Then I slept")

    def test_full_pipeline_numbers(self):
        self.assertEqual(_pp().post_grammar("i spent five dollars"), "I spent $5")

    def test_format_numbers_toggle_off(self):
        self.assertEqual(_pp(format_numbers=False).post_grammar("i spent five dollars"),
                         "I spent five dollars")


class TestModelResolution(unittest.TestCase):
    def test_auto_gpu(self):
        self.assertEqual(_resolve_model_name("auto", "cuda", "transcribe"), "large-v3-turbo")
        self.assertEqual(_resolve_model_name("auto", "cuda", "translate"), "large-v3-turbo")

    def test_auto_cpu(self):
        self.assertEqual(_resolve_model_name("auto", "cpu", "transcribe"), "small.en")
        self.assertEqual(_resolve_model_name("auto", "cpu", "translate"), "small")

    def test_explicit_passthrough(self):
        self.assertEqual(_resolve_model_name("medium.en", "cuda", "transcribe"), "medium.en")

    def test_build_hotwords(self):
        cfg = _deep_merge(DEFAULTS, {"postprocess": {"replacements":
              {"github": "GitHub", "voicetype": "VoiceType"}}})
        hw = _build_hotwords(cfg)
        self.assertIn("GitHub", hw)
        self.assertIn("VoiceType", hw)


class TestConfig(unittest.TestCase):
    def test_default_model_is_auto(self):
        self.assertEqual(DEFAULTS["model"]["name"], "auto")

    def test_deep_merge(self):
        merged = _deep_merge({"a": {"x": 1, "y": 2}}, {"a": {"y": 9}})
        self.assertEqual(merged["a"], {"x": 1, "y": 9})

    def test_load_config_tolerates_utf8_bom(self):
        # Windows Notepad saves UTF-8 with a BOM; load_config must still read it
        # rather than silently falling back to defaults.
        import json
        import os
        import tempfile
        from pathlib import Path
        from voicetype.config import load_config
        fd, p = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            Path(p).write_text(json.dumps({"model": {"name": "tiny.en"}}),
                               encoding="utf-8-sig")
            cfg = load_config(Path(p))
            self.assertEqual(cfg["model"]["name"], "tiny.en")
        finally:
            os.remove(p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
