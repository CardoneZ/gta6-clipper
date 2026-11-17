"""Signal math, moment detectors on synthetic signals, and FFmpeg streaming readers on generated media."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import numpy as np

import helpers
from core_engine import activity
from core_engine.activity import ActivityConfig, Signals, detect_moments
from core_engine.chunking import Chunk

HOP = 0.1


def base(seconds: float, audio_db: float = -40.0, motion: float = 2.0, seed: int = 7) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    n = int(seconds / HOP)
    return (audio_db + rng.normal(0, 1.0, n)).astype(np.float32), (motion + rng.normal(0, 0.3, n)).clip(0).astype(np.float32)


def tags_of(moments: list[dict]) -> list[str]:
    return [t for m in moments for t in m["tags"]]


class SignalMathTests(unittest.TestCase):
    def test_hop_rms_db_full_scale_and_silence(self) -> None:
        loud = np.full(800, 32767, dtype=np.int16)
        quiet = np.zeros(800, dtype=np.int16)
        out = activity.hop_rms_db(np.concatenate((loud, quiet, quiet[:100])), 800)
        self.assertEqual(out.size, 2)  # trailing partial hop dropped
        self.assertAlmostEqual(float(out[0]), 0.0, delta=0.01)
        self.assertEqual(float(out[1]), activity.SILENCE_DB)

    def test_robust_z_ignores_outliers(self) -> None:
        x = np.array([1, 1, 1, 1, 1, 1000], dtype=np.float32)
        z = activity.robust_z(x, floor=1.0)
        self.assertEqual(float(z[0]), 0.0)
        self.assertGreater(float(z[-1]), 500)

    def test_onsets_collapse_rising_edge(self) -> None:
        a = np.full(50, -40.0, dtype=np.float32)
        a[20:25] = -10.0
        hits = activity.onsets(a, 9.0, 3)
        self.assertEqual(hits.tolist(), [20])

    def test_resample_motion_length_and_empty(self) -> None:
        self.assertEqual(activity.resample_motion(np.array([1.0, 2.0, 3.0]), 4, HOP, 30).size, 30)
        self.assertEqual(float(activity.resample_motion(np.zeros(0), 4, HOP, 5).sum()), 0.0)


class DetectorTests(unittest.TestCase):
    def test_quiet_signal_has_no_moments(self) -> None:
        a, m = base(120)
        self.assertEqual(detect_moments(Signals(0.0, HOP, a, m)), [])

    def test_car_crash(self) -> None:
        a, m = base(60)
        i = int(30 / HOP)
        a[i:i + 4] = -5.0
        m[i:i + 3] = 40.0
        out = detect_moments(Signals(100.0, HOP, a, m))
        crash = [x for x in out if "CAR_CRASH" in x["tags"]]
        self.assertEqual(len(crash), 1)
        self.assertAlmostEqual(crash[0]["peak_sec"], 130.05, delta=0.2)  # absolute time (t0 = 100)
        self.assertLessEqual(crash[0]["start_sec"], 130.0)
        self.assertGreaterEqual(crash[0]["end_sec"], 133.0)

    def test_clutch_kill_vs_gunfight(self) -> None:
        for quiet_after, want in ((True, "CLUTCH_KILL"), (False, "GUNFIGHT")):
            with self.subTest(want=want):
                a, m = base(60, audio_db=-30.0)
                for t in (20.0, 21.0, 22.0, 23.0):
                    i = int(t / HOP)
                    a[i:i + 2] = -3.0
                after = slice(int(23.3 / HOP), int(27 / HOP))
                a[after] = -60.0 if quiet_after else -15.0
                out = detect_moments(Signals(0.0, HOP, a, m))
                self.assertIn(want, tags_of(out))
                other = "GUNFIGHT" if want == "CLUTCH_KILL" else "CLUTCH_KILL"
                self.assertNotIn(other, tags_of(out))

    def test_police_chase_sustained(self) -> None:
        a, m = base(120)
        s, e = int(40 / HOP), int(60 / HOP)
        a[s:e] = -28.0
        m[s:e] = 12.0
        out = detect_moments(Signals(0.0, HOP, a, m))
        chase = [x for x in out if "POLICE_CHASE" in x["tags"]]
        self.assertEqual(len(chase), 1)
        self.assertGreaterEqual(chase[0]["signal"]["sustained_sec"], 8.0)

    def test_short_burst_is_not_a_chase(self) -> None:
        a, m = base(120)
        s, e = int(40 / HOP), int(44 / HOP)
        a[s:e] = -28.0
        m[s:e] = 12.0
        self.assertNotIn("POLICE_CHASE", tags_of(detect_moments(Signals(0.0, HOP, a, m))))

    def test_reaction_on_calm_screen(self) -> None:
        a, m = base(60, motion=2.0)
        s = int(30 / HOP)
        a[s - 10:s + 15] = np.linspace(-40, -15, 25)  # gradual swell: no sharp onset
        out = detect_moments(Signals(0.0, HOP, a, m))
        self.assertIn("REACTION", tags_of(out))
        self.assertNotIn("CAR_CRASH", tags_of(out))

    def test_scores_bounded_and_moments_capped(self) -> None:
        a, m = base(200)
        a[:] = -5.0
        m[:] = 50.0
        a[::7] = -60.0  # endless onsets everywhere
        cfg = ActivityConfig(max_moment_sec=40.0)
        for x in detect_moments(Signals(0.0, HOP, a, m), cfg):
            self.assertLessEqual(x["score"], 100.0)
            self.assertLessEqual(x["end_sec"] - x["start_sec"], 40.0 + 1e-6)

    def test_merge_overlapping_unions_tags(self) -> None:
        merged = activity.merge_moments([
            {"start_sec": 0, "end_sec": 10, "peak_sec": 5, "score": 60, "tags": ["CAR_CRASH"], "signal": {}},
            {"start_sec": 8, "end_sec": 20, "peak_sec": 15, "score": 70, "tags": ["POLICE_CHASE"], "signal": {}},
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["tags"], ["CAR_CRASH", "POLICE_CHASE"])
        self.assertEqual(merged[0]["score"], 73.0)
        self.assertEqual(merged[0]["peak_sec"], 15)

    def test_analyze_chunk_sanitises_nan(self) -> None:
        a, m = base(30)
        a[5] = np.nan
        out = activity.analyze_chunk("ffmpeg", "x", Chunk(0, 0.0, 30.0, 0.0, 30.0),
                                     audio_reader=lambda *_: a, motion_reader=lambda *_: m)
        self.assertIsInstance(out, list)


@unittest.skipUnless(helpers.FFMPEG, "ffmpeg not on PATH")
class FfmpegReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_readers_and_end_to_end_crash(self) -> None:
        src = helpers.make_crash_video(self.dir / "crash.mp4", duration=20.0, crash_at=10.0)
        audio = activity.read_audio_db(helpers.FFMPEG, str(src), 0.0, 20.0)
        self.assertEqual(audio.size, 200)
        self.assertGreater(float(audio[103]) - float(audio[50]), 20.0)
        motion = activity.read_motion(helpers.FFMPEG, str(src), 0.0, 20.0)
        self.assertGreater(float(motion.max()), 50.0)
        self.assertLess(float(np.median(motion)), 1.0)
        moments = activity.analyze_chunk(helpers.FFMPEG, str(src), Chunk(0, 0.0, 20.0, 0.0, 20.0))
        crash = [x for x in moments if "CAR_CRASH" in x["tags"]]
        self.assertEqual(len(crash), 1)
        self.assertAlmostEqual(crash[0]["peak_sec"], 10.0, delta=0.5)

    def test_seeked_window_uses_absolute_times(self) -> None:
        src = helpers.make_crash_video(self.dir / "crash.mp4", duration=20.0, crash_at=14.0)
        moments = activity.analyze_chunk(helpers.FFMPEG, str(src), Chunk(1, 8.0, 20.0, 10.0, 20.0))
        crash = [x for x in moments if "CAR_CRASH" in x["tags"]]
        self.assertEqual(len(crash), 1)
        self.assertAlmostEqual(crash[0]["peak_sec"], 14.0, delta=0.5)

    def test_no_audio_track_reads_as_silence(self) -> None:
        src = helpers.make_silent_video(self.dir / "silent.mp4", duration=4.0)
        audio = activity.read_audio_db(helpers.FFMPEG, str(src), 0.0, 4.0)
        self.assertEqual(audio.size, 40)
        self.assertTrue(np.all(audio == activity.SILENCE_DB))


if __name__ == "__main__":
    unittest.main()
