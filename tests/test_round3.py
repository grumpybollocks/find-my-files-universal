"""Round 3 (2026-11-10): 'Related files' (versions + parts of one project).
Name patterns below are taken from real project file names.

Run:  QT_QPA_PLATFORM=offscreen python3 -I -m unittest discover -s tests -v
"""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safe_env  # noqa: E402,F401  (temp picture cache, no builder)

from PySide6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])

import indexer  # noqa: E402
import search  # noqa: E402
from test_round2 import TempIndex  # noqa: E402


class Family(unittest.TestCase):
    def proj(self, n):
        return search.family(n)[1]

    def test_parts_of_one_project_group(self):
        names = ["corp cruce celtica (repaired) x2.stl", "cheita cruce celtica (repaired).stl",
                 "spate cruce celtica 2 (repaired) (self-intersections removed)(1).stl",
                 "clopotel cruce celtica (repaired) 666 (repaired).lys"]
        self.assertEqual({self.proj(n) for n in names}, {"cruce celtica"})

    def test_version_numbers_with_hyphen_are_kept(self):
        self.assertEqual(self.proj("cruce 4-1 baza2 (repaired).stl"), "cruce 4-1")
        self.assertNotEqual(self.proj("cruce 4-1 baza2.stl"), self.proj("cruce 4-2 spate.stl"))

    def test_slicer_noise_removed(self):
        self.assertEqual(self.proj("corp cruce trifoi (repaired) x2 2222 222(repaired).lys"), "cruce trifoi")

    def test_one_generic_word_keeps_numbers(self):
        # "Cruce 3 ..." and "Cruce 2 ..." are different models: never merge them into "cruce"
        self.assertEqual(self.proj("Cruce 3 63 top.stl"), "cruce 3 63")
        self.assertNotEqual(self.proj("Cruce 3 63 top.stl"), self.proj("Cruce 2 63.stl"))

    def test_diacritics(self):
        self.assertEqual(self.proj("spate cruciuliță.stl"), self.proj("corp cruciulita.stl"))


class RelatedSearch(TempIndex):
    def test_related_across_types_and_folders(self):
        os.makedirs(os.path.join(self.work, "a"))
        os.makedirs(os.path.join(self.work, "b"))
        for p in ("a/corp cruce celtica (repaired).stl", "a/cheita cruce celtica.stl",
                  "b/corp cruce celtica (repaired) x2.lys", "a/cruce celtica mare noua.stl",
                  "a/corp cruce trifoi.stl"):
            with open(os.path.join(self.work, p), "w") as f:
                f.write("x")
        indexer.build()
        key, rows = search.related("spate cruce celtica 2 (repaired).stl")
        self.assertEqual(key, "cruce celtica")
        self.assertEqual(sorted(r[1] for r in rows), ["cheita cruce celtica.stl",
                                                       "corp cruce celtica (repaired) x2.lys",
                                                       "corp cruce celtica (repaired).stl"])

    def test_window_related_button(self):
        import findfiles
        for n in ("corp cruce celtica.stl", "cheita cruce celtica.stl", "corp cruce trifoi.stl"):
            with open(os.path.join(self.work, n), "w") as f:
                f.write("x")
        w = findfiles.Main()
        end = time.time() + 15  # wait for the file list (a fixed 2 s was flaky)
        while not w._index_msg and time.time() < end:
            APP.processEvents(); time.sleep(0.02)
        w.q.setText("corp cruce celtica"); w.run_search()
        w.table.selectRow(0)
        w.show_related()
        self.assertEqual(w._mode, "related")
        self.assertEqual(w.table.rowCount(), 2)
        w.on_indexed(3, [])  # a background refresh must not throw the related view away
        self.assertEqual(w._mode, "related")
        self.assertEqual(w.table.rowCount(), 2)
        w.close()


if __name__ == "__main__":
    unittest.main()
