import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import film_preflight as fp

HAVE_FF = shutil.which("ffmpeg") and shutil.which("ffprobe")


@unittest.skipUnless(HAVE_FF, "needs ffmpeg")
class OnePassMatchesSeparateCalls(unittest.TestCase):
    """The core promise: one decode gives the same numbers as one ffmpeg call per check (cinecode qc.py's arguments)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.film = fp.make_demo(os.path.join(cls.tmp, "demo.mp4"))
        cls.info, cls.one = fp.one_pass(cls.film, os.path.join(cls.tmp, "one"))
        cls.sep = fp.separate(cls.film, os.path.join(cls.tmp, "sep"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_same_values(self):
        for key in ("hook", "black", "freeze", "still", "dups", "frames", "bright", "cuts", "loud", "silence"):
            with self.subTest(key=key):
                self.assertEqual(self.one[key], self.sep[key])

    def test_finds_the_planted_faults(self):
        self.assertEqual([(3.0, 4.0)], [(round(s, 2), round(e, 2)) for s, e, _ in self.one["black"]])
        self.assertTrue(any(abs(s - 4.03) < 0.05 and abs(d - 4.03) < 0.05 for s, d in self.one["freeze"]))
        self.assertTrue(any(d >= 3 for _, d in self.one["still"]))
        self.assertEqual([(5.0, 7.0)], [(round(s, 1), round(e, 1)) for s, e, _ in self.one["silence"]])
        self.assertIn(8.07, [round(t, 2) for t in self.one["cuts"]])
        self.assertEqual(420, self.one["frames"])

    def test_artifacts(self):
        for f in ("sheet.jpg", "spectrogram.png", "sheet-index.txt"):
            self.assertTrue(os.path.getsize(os.path.join(self.tmp, "one", f)) > 0, f)

    def test_check_exit_code_and_report(self):
        out = os.path.join(self.tmp, "chk")
        rc = fp.check(self.film, out)
        self.assertEqual(0, rc)
        text = open(os.path.join(out, "report.txt"), encoding="utf-8").read()
        self.assertIn("WARN  long stills", text)
        self.assertIn("WARN  black gaps", text)


class Verdicts(unittest.TestCase):
    info = {"audio": True, "duration": 20.0}

    def raw(self, **kw):
        r = {"hook": [1.0, 1.2, 0.9], "black": [], "freeze": [], "still": [], "dups": [], "frames": 600,
             "bright": [120.0], "cuts": [], "loud": {"I": -14.2, "LRA": 5.0, "peak": -1.5}, "silence": []}
        r.update(kw)
        return {c: s for s, c, _ in fp.verdicts(self.info, r)}

    def test_clean_film_passes(self):
        v = self.raw()
        self.assertNotIn("FAIL", v.values())
        self.assertEqual("PASS", v["loudness"])

    def test_still_open_fails_hook(self):
        self.assertEqual("FAIL", self.raw(hook=[0.1, 0.0, 0.2])["hook"])
        self.assertEqual("FAIL", self.raw(hook=[])["hook"])

    def test_drawn_on_twos_passes_hook(self):
        self.assertEqual("PASS", self.raw(hook=[1.5, 0.0, 1.5, 0.0, 1.5])["hook"])

    def test_black_open_fails(self):
        self.assertEqual("FAIL", self.raw(black=[(0.0, 0.5, 0.5)])["black open"])

    def test_long_still_rules(self):
        self.assertEqual("WARN", self.raw(still=[(2.0, 4.0)])["long stills"])
        self.assertEqual("FAIL", self.raw(still=[(2.0, 12.0)])["long stills"])

    def test_loudness_out_of_target_warns(self):
        self.assertEqual("WARN", self.raw(loud={"I": -20.0, "LRA": 3.0, "peak": -3.0})["loudness"])
        self.assertEqual("WARN", self.raw(loud={"I": -14.0, "LRA": 3.0, "peak": -0.2})["loudness"])

    def test_parse_open_silence_and_freeze_at_end(self):
        err = ("[freezedetect@still @ 0] lavfi.freezedetect.freeze_start: 15\n"
               "[silencedetect @ 0] silence_start: 18.5\n")
        r = fp.parse(err, "", "", "", "", {"duration": 20.0})
        self.assertEqual([(15.0, 5.0)], r["still"])
        self.assertEqual([(18.5, 20.0, 1.5)], r["silence"])


if __name__ == "__main__":
    unittest.main()
