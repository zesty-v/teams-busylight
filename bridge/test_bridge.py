"""Unit tests for the pure helpers in teams_light_bridge.py.

Run with:  python3 -m unittest test_bridge -v   (works in WSL too)
"""

import os
import tempfile
import time
import unittest
from pathlib import Path

import teams_light_bridge as bridge


SAMPLE = ("2026-08-07T19:05:12.263235+02:00 0x00008ea8 <INFO> "
          "TaskbarService: SetTaskbarIconOverlay overlay description:"
          "No items, status {status}\n")


class ParseStatusTest(unittest.TestCase):
    def test_real_log_lines(self):
        self.assertEqual(
            bridge.parse_status(SAMPLE.format(status="Available")),
            "Available")
        self.assertEqual(
            bridge.parse_status(
                "SetTaskbarIconOverlay overlay description:0 items, "
                "status Away"),
            "Away")

    def test_multi_word_statuses(self):
        # New Teams logs human-readable strings: "status Do not disturb"
        self.assertEqual(
            bridge.parse_status(SAMPLE.format(status="Do not disturb")),
            "Do not disturb")
        self.assertEqual(
            bridge.parse_status(
                "SetTaskbarIconOverlay overlay description:0 items, "
                "status In a call"),
            "In a call")

    def test_non_status_lines(self):
        self.assertIsNone(bridge.parse_status(
            "TaskbarBadgeServicePackaged: Setting badge NoBadge"))
        self.assertIsNone(bridge.parse_status(
            "SetTaskbarIconOverlay overlay description: "))
        self.assertIsNone(bridge.parse_status(""))

    def test_busy_classification(self):
        for status in ("Busy", "DoNotDisturb", "Do not disturb",
                       "do not disturb", "InACall", "In a call",
                       "InAMeeting", "In a meeting", "OnThePhone",
                       "Presenting", "InAConferenceCall"):
            self.assertTrue(bridge.is_busy_status(status), status)
        for status in ("Available", "Away", "BeRightBack",
                       "Be right back", "Appear offline", "Offline",
                       "Unknown", ""):
            self.assertFalse(bridge.is_busy_status(status), status)


class LogFileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str, *statuses: str, age: float = 0) -> Path:
        path = self.dir / name
        path.write_text(
            "noise line\n"
            + "".join(SAMPLE.format(status=s) for s in statuses)
            + "more noise\n")
        if age:
            t = time.time() - age
            os.utime(path, (t, t))
        return path

    def test_newest_log(self):
        self._write("MSTeams_old.log", "Available", age=100)
        new = self._write("MSTeams_new.log", "Busy")
        self.assertEqual(bridge.newest_log(self.dir), new)

    def test_newest_log_empty_dir(self):
        self.assertIsNone(bridge.newest_log(self.dir))
        self.assertIsNone(bridge.newest_log(self.dir / "does-not-exist"))

    def test_last_status_in_file(self):
        path = self._write("MSTeams_a.log", "Available", "Busy", "Away")
        self.assertEqual(bridge.last_status_in_file(path), "Away")

    def test_initial_status_prefers_newest_file(self):
        self._write("MSTeams_old.log", "Busy", age=100)
        self._write("MSTeams_new.log", "Available")
        self.assertEqual(bridge.initial_status(self.dir), "Available")

    def test_initial_status_falls_back_to_older_file(self):
        self._write("MSTeams_old.log", "Busy", age=100)
        (self.dir / "MSTeams_new.log").write_text("no status here\n")
        self.assertEqual(bridge.initial_status(self.dir), "Busy")

    def test_initial_status_empty(self):
        self.assertIsNone(bridge.initial_status(self.dir))


if __name__ == "__main__":
    unittest.main()
