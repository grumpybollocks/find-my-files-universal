"""Drives under /mnt must count as 'unplugged' when not mounted (their last files are kept)."""
import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import indexer


class DriveMissing(unittest.TestCase):
    def test_mnt_drive_not_mounted(self):
        with mock.patch("os.path.ismount", return_value=False) as m:
            self.assertTrue(indexer._drive_missing("/mnt/DISK1/work stuff"))
            m.assert_called_with("/mnt/DISK1")

    def test_mnt_drive_mounted(self):
        with mock.patch("os.path.ismount", return_value=True):
            self.assertFalse(indexer._drive_missing("/mnt/DISK1/work stuff"))

    def test_run_media_still_works(self):
        with mock.patch("os.path.ismount", return_value=False) as m:
            self.assertTrue(indexer._drive_missing("/run/media/u/Storage/Models"))
            m.assert_called_with("/run/media/u/Storage")

    def test_home_never_missing(self):
        self.assertFalse(indexer._drive_missing(os.path.expanduser("~/Desktop")))

    def test_a_never_drive_and_game_folders_are_not_indexed(self):
        import roots as R
        s = R._clean({"roots": [R._r("/mnt/WINDRIVE/Users", "Win"), R._r("/mnt/DISK1/work", "Disk1 › work")],
                      "never": ["/mnt/WINDRIVE"]})
        ok, why = R.check("/mnt/WINDRIVE/Users", s["never"])
        self.assertFalse(ok); self.assertTrue(why)                            # refused, with a reason
        self.assertFalse(R.check("/mnt/DISK1/SteamLibrary/steamapps", s["never"])[0])
        import indexer
        before = indexer.NEVER
        try:
            indexer.NEVER = tuple(s["never"])
            self.assertTrue(indexer.blocked("/mnt/WINDRIVE/Users/x"))
            self.assertFalse(indexer.blocked("/mnt/DISK1/work"))
        finally:
            indexer.NEVER = before


if __name__ == "__main__":
    unittest.main()
