"""Unit tests for the pure helpers in teams_light_bridge.py.

Run with:  python3 -m unittest test_bridge -v   (works in WSL too)
"""

import os
import tempfile
import time
import unittest
from pathlib import Path

import teams_light_bridge as bridge


# Real line shapes captured from new Teams logs.
ACTION = ("2026-08-10T06:53:35.976887+02:00 0x000040e0 <INFO> "
          "native_modules::UserDataCrossCloudModule: Received Action: "
          "UserPresenceAction: {{cloud_context: {cloud}, "
          "availability: {status}}}\n")
OVERLAY = ("2026-08-07T19:05:12.263235+02:00 0x00008ea8 <INFO> "
           "TaskbarService: SetTaskbarIconOverlay overlay description:"
           "No items, status {status}\n")


class ParsePresenceTest(unittest.TestCase):
    def test_user_presence_action(self):
        self.assertEqual(
            bridge.parse_presence(ACTION.format(
                cloud="https://teams.microsoft.com", status="Busy")),
            ("https://teams.microsoft.com", "Busy"))
        self.assertEqual(
            bridge.parse_presence(ACTION.format(
                cloud="https://teams.live.com", status="Available")),
            ("https://teams.live.com", "Available"))

    def test_legacy_overlay(self):
        self.assertEqual(
            bridge.parse_presence(OVERLAY.format(status="Available")),
            ("taskbar-overlay", "Available"))
        self.assertEqual(
            bridge.parse_presence(OVERLAY.format(status="Do not disturb")),
            ("taskbar-overlay", "Do not disturb"))

    def test_non_presence_lines(self):
        self.assertIsNone(bridge.parse_presence(
            "TaskbarBadgeServicePackaged: Setting badge NoBadge"))
        self.assertIsNone(bridge.parse_presence(
            "SetTaskbarIconOverlay overlay description:"
            "Requires your attention"))
        self.assertIsNone(bridge.parse_presence(""))

    def test_busy_classification(self):
        for status in ("Busy", "DoNotDisturb", "Do not disturb",
                       "do not disturb", "InACall", "In a call",
                       "InAMeeting", "In a meeting", "OnThePhone",
                       "Presenting", "InAConferenceCall"):
            self.assertTrue(bridge.is_busy_status(status), status)
        for status in ("Available", "Away", "BeRightBack",
                       "Be right back", "Appear offline", "Offline",
                       "PresenceUnknown", "Unknown", ""):
            self.assertFalse(bridge.is_busy_status(status), status)

    def test_any_busy(self):
        self.assertFalse(bridge.any_busy({}))
        self.assertFalse(bridge.any_busy(
            {"https://teams.microsoft.com": "Available"}))
        self.assertTrue(bridge.any_busy(
            {"https://teams.microsoft.com": "Busy",
             "https://teams.live.com": "Available"}))


class LogFileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str, *lines: str, age: float = 0) -> Path:
        path = self.dir / name
        path.write_text("noise line\n" + "".join(lines) + "more noise\n")
        if age:
            t = time.time() - age
            os.utime(path, (t, t))
        return path

    def test_newest_log(self):
        self._write("MSTeams_old.log", age=100)
        new = self._write("MSTeams_new.log")
        self.assertEqual(bridge.newest_log(self.dir), new)

    def test_newest_log_empty_dir(self):
        self.assertIsNone(bridge.newest_log(self.dir))
        self.assertIsNone(bridge.newest_log(self.dir / "does-not-exist"))

    def test_initial_clouds_tracks_per_cloud(self):
        # Work went Busy, then personal went Available afterwards: the
        # work Busy must survive (any_busy stays True).
        self._write(
            "MSTeams_a.log",
            ACTION.format(cloud="https://teams.microsoft.com",
                          status="Busy"),
            ACTION.format(cloud="https://teams.live.com",
                          status="Available"))
        clouds = bridge.initial_clouds(self.dir)
        self.assertEqual(clouds, {"https://teams.microsoft.com": "Busy",
                                  "https://teams.live.com": "Available"})
        self.assertTrue(bridge.any_busy(clouds))

    def test_initial_clouds_later_line_wins_per_cloud(self):
        self._write(
            "MSTeams_a.log",
            ACTION.format(cloud="https://teams.microsoft.com",
                          status="Busy"),
            ACTION.format(cloud="https://teams.microsoft.com",
                          status="Available"))
        self.assertEqual(
            bridge.initial_clouds(self.dir),
            {"https://teams.microsoft.com": "Available"})

    def test_initial_clouds_spans_files_oldest_first(self):
        # Older file has work=Busy; newer file only mentions personal.
        # Both must be present; newer file must not erase older state.
        self._write("MSTeams_old.log",
                    ACTION.format(cloud="https://teams.microsoft.com",
                                  status="Busy"),
                    age=100)
        self._write("MSTeams_new.log",
                    ACTION.format(cloud="https://teams.live.com",
                                  status="Away"))
        self.assertEqual(
            bridge.initial_clouds(self.dir),
            {"https://teams.microsoft.com": "Busy",
             "https://teams.live.com": "Away"})

    def test_initial_clouds_empty(self):
        self.assertEqual(bridge.initial_clouds(self.dir), {})


if __name__ == "__main__":
    unittest.main()
