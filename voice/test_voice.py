#!/usr/bin/env python3
"""
Tests for the voice shell's UI.

The ear toggle failed silently in Firefox once: the button was disabled at boot
with no explanation, so clicking it did nothing at all and there was no way to
find out why. These tests pin the two things that made that possible — broken
inline JavaScript, and element ids referenced but never defined — plus the rule
that the ear must never be disabled.

    python voice/test_voice.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

VOICE_DIR = Path(__file__).resolve().parent
INDEX = VOICE_DIR / "index.html"
SCRIPT_RE = re.compile(r"<script>\s*(.*?)\s*</script>", re.DOTALL)


def read_index() -> str:
    return INDEX.read_text(encoding="utf-8")


def inline_script() -> str:
    match = SCRIPT_RE.search(read_index())
    if not match:
        raise AssertionError("index.html has no inline <script> block")
    return match.group(1)


class TestUiStructure(unittest.TestCase):

    def test_index_exists(self):
        self.assertTrue(INDEX.is_file(), "voice/index.html is missing")

    def test_inline_script_present(self):
        self.assertGreater(len(inline_script()), 1000)

    def test_every_referenced_element_id_exists(self):
        # A getElementById for an id that is not in the markup yields null, and
        # the first property access on it throws, killing the whole script.
        html = read_index()
        referenced = set(re.findall(r'getElementById\("([^"]+)"\)', html))
        self.assertTrue(referenced, "no elements looked up at all")
        for element_id in sorted(referenced):
            self.assertIn(f'id="{element_id}"', html,
                          f'getElementById("{element_id}") has no matching element')

    def test_ear_is_never_disabled(self):
        # The regression. A disabled control cannot explain itself.
        script = inline_script()
        self.assertNotIn("el.ear.disabled = true", script,
                         "the ear must stay clickable so it can report why "
                         "speech is unavailable")
        self.assertNotIn("el.ear.disabled=true", script)

    def test_ear_reports_unsupported_browsers(self):
        script = inline_script()
        self.assertIn("speechSupported", script)
        self.assertIn("browserName", script)
        # The unsupported path must produce a message, not a bare return.
        self.assertRegex(script, r"can't do speech recognition")

    def test_microphone_failures_are_each_named(self):
        script = inline_script()
        for error_name in ("NotAllowedError", "NotFoundError",
                           "NotReadableError", "SecurityError"):
            self.assertIn(error_name, script,
                          f"{error_name} would fail without a useful message")

    def test_secure_origin_is_explained(self):
        self.assertIn("isSecureContext", inline_script())

    def test_job_polling_exists(self):
        # Answers take seconds; the page must poll rather than block.
        script = inline_script()
        self.assertIn("/api/ask", script)
        self.assertIn("/api/job/", script)

    def test_wake_word_stripping(self):
        script = inline_script()
        self.assertIn("WAKE_STRIP", script)
        self.assertRegex(script, r"jarvis")


class TestJavaScriptParses(unittest.TestCase):
    """A syntax error in the inline script breaks every control on the page."""

    def test_inline_script_is_valid_javascript(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not on PATH — cannot syntax-check the UI")

        # mkstemp leaves the descriptor open, and Windows will not delete a file
        # that is still open, so close it before writing.
        handle, path = tempfile.mkstemp(suffix=".mjs")
        os.close(handle)
        try:
            Path(path).write_text(inline_script(), encoding="utf-8")
            result = subprocess.run([node, "--check", path],
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    text=True, timeout=60)
            self.assertEqual(result.returncode, 0,
                             f"inline JS failed to parse:\n{result.stderr}")
        finally:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass


class TestServerModule(unittest.TestCase):
    """run_headless.py must import cleanly — the server imports it."""

    def test_imports_and_exposes_expected_helpers(self):
        sys.path.insert(0, str(VOICE_DIR))
        import run_headless

        for name in ("ask", "brain_briefing", "compose_task", "split_output",
                     "find_dsh_bin", "locate_brain"):
            self.assertTrue(hasattr(run_headless, name), f"missing {name}")

    def test_compose_task_includes_the_briefing(self):
        sys.path.insert(0, str(VOICE_DIR))
        import run_headless

        composed = run_headless.compose_task("what do we charge?", "## Rate card\n$95")
        self.assertIn("what do we charge?", composed)
        self.assertIn("$95", composed)
        self.assertIn("second-brain", composed)

    def test_compose_task_without_briefing_is_untouched(self):
        sys.path.insert(0, str(VOICE_DIR))
        import run_headless

        self.assertEqual(run_headless.compose_task("hello", ""), "hello")

    def test_split_output_separates_reasoning_from_answer(self):
        sys.path.insert(0, str(VOICE_DIR))
        import run_headless

        raw = "dsh: reasoning:\nthinking about it\n\nThe answer is 42."
        answer, reasoning = run_headless.split_output(raw)
        self.assertEqual(answer, "The answer is 42.")
        self.assertIn("thinking about it", reasoning)

    def test_split_output_passes_through_a_plain_answer(self):
        sys.path.insert(0, str(VOICE_DIR))
        import run_headless

        answer, _ = run_headless.split_output("Just the answer.")
        self.assertEqual(answer, "Just the answer.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
