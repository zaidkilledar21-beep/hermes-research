"""Tests for pipeline/envfile.py — the stale-parent-environment fix.

The contract: fill only what is ABSENT, never clobber an explicit export, and never let a
Windows-edited file inject a trailing CR into a value (the exact corruption that revoked-key
incident turned on).
"""
import os
import tempfile
import unittest
from pathlib import Path

from pipeline import envfile


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / ".env"
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        self.dir.cleanup()

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")

    def test_sets_absent_vars(self):
        os.environ.pop("ENVFILE_T_A", None)
        self.write("ENVFILE_T_A=hello\n")
        n = envfile.load(self.path)
        self.assertEqual(n, 1)
        self.assertEqual(os.environ["ENVFILE_T_A"], "hello")

    def test_never_overrides_explicit_export(self):
        os.environ["ENVFILE_T_B"] = "from-parent"
        self.write("ENVFILE_T_B=from-file\n")
        envfile.load(self.path)
        self.assertEqual(os.environ["ENVFILE_T_B"], "from-parent")

    def test_override_flag_does_override(self):
        os.environ["ENVFILE_T_C"] = "from-parent"
        self.write("ENVFILE_T_C=from-file\n")
        envfile.load(self.path, override=True)
        self.assertEqual(os.environ["ENVFILE_T_C"], "from-file")

    def test_value_containing_equals_survives(self):
        os.environ.pop("ENVFILE_T_D", None)
        self.write("ENVFILE_T_D=abc==def=\n")
        envfile.load(self.path)
        self.assertEqual(os.environ["ENVFILE_T_D"], "abc==def=")

    def test_trailing_cr_stripped(self):
        os.environ.pop("ENVFILE_T_E", None)
        self.path.write_bytes(b"ENVFILE_T_E=sk-or-v1-deadbeef\r\n")
        envfile.load(self.path)
        self.assertEqual(os.environ["ENVFILE_T_E"], "sk-or-v1-deadbeef")
        self.assertNotIn("\r", os.environ["ENVFILE_T_E"])

    def test_comments_blanks_and_junk_skipped(self):
        os.environ.pop("ENVFILE_T_F", None)
        self.write("# a comment\n\n   \nnot_a_pair\nENVFILE_T_F=ok\n")
        envfile.load(self.path)
        self.assertEqual(os.environ["ENVFILE_T_F"], "ok")

    def test_quotes_stripped(self):
        os.environ.pop("ENVFILE_T_G", None)
        os.environ.pop("ENVFILE_T_H", None)
        self.write("ENVFILE_T_G=\"quoted\"\nENVFILE_T_H='single'\n")
        envfile.load(self.path)
        self.assertEqual(os.environ["ENVFILE_T_G"], "quoted")
        self.assertEqual(os.environ["ENVFILE_T_H"], "single")

    def test_export_prefix_tolerated(self):
        os.environ.pop("ENVFILE_T_I", None)
        self.write("export ENVFILE_T_I=yes\n")
        envfile.load(self.path)
        self.assertEqual(os.environ.get("ENVFILE_T_I"), "yes")

    def test_missing_file_is_a_noop(self):
        self.assertEqual(envfile.load(Path(self.dir.name) / "nope.env"), 0)

    def test_the_actual_regression_flag_would_be_picked_up(self):
        # runs 47-49 executed with this absent while .env on the same box set it to 1
        os.environ.pop("PLANNER_ENABLED", None)
        self.write("PLANNER_ENABLED=1\n")
        envfile.load(self.path)
        self.assertEqual(os.environ["PLANNER_ENABLED"], "1")


if __name__ == "__main__":
    unittest.main()
