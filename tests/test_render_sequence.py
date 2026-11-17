"""`VideoEngine.render_sequence`: narration, TTS, window fitting, template wiring, failure isolation, real render."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import helpers
from core_engine import engine as engine_mod
from core_engine.engine import RenderOptions, VideoEngine, load_clipfarm, pick_sfx, safe_stem, soundboard_mix_cmd
from saas import app as app_mod

MOMENTS = [
    {"start_sec": 100.0, "end_sec": 108.0, "peak_sec": 104.0, "score": 80.0, "tags": ["CAR_CRASH"]},
    {"start_sec": 400.0, "end_sec": 410.0, "peak_sec": 405.0, "score": 70.0, "tags": ["POLICE_CHASE", "GUNFIGHT"]},
]


class FakeComposer:
    def __init__(self, fail_on: int | None = None) -> None:
        self.renders: list[dict] = []
        self.fail_on = fail_on
        real = load_clipfarm("composer")
        self.long_form_window = real.long_form_window
        self.fit_window_to_narration = real.fit_window_to_narration

    def render_vertical_clip(self, ffmpeg, ffprobe, source, out_dir, name, start, end, preset, **kw):  # type: ignore[no-untyped-def]
        if self.fail_on is not None and len(self.renders) == self.fail_on:
            self.renders.append({"failed": True})
            raise RuntimeError("nvenc out of memory")
        self.renders.append({"name": name, "start": start, "end": end, **kw})
        path = Path(out_dir) / name
        path.write_bytes(b"mp4")
        return {"output_path": str(path), "output_filename": name, "verified_duration_sec": end - start}


class FakeTts:
    def __init__(self, seconds: float = 12.0, ok: bool = True) -> None:
        self.calls: list[dict] = []
        self.seconds, self.ok = seconds, ok

    def synthesize_tts(self, text, wav, **kw):  # type: ignore[no-untyped-def]
        self.calls.append({"text": text, "wav": wav, **kw})
        if not self.ok:
            return {"synthesized": False, "reason": "no engine"}
        Path(wav).write_bytes(b"RIFF")
        return {"synthesized": True, "duration_sec": self.seconds, "provider": kw.get("provider"),
                "word_timings": [{"word": "hi", "start": 0.0, "end": 0.3}]}

    def discover_rvc_voices(self, root):  # type: ignore[no-untyped-def]
        return [{"id": "rvc:Test", "label": "RVC: Test", "lang": "en"}]


class FakeCaptions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def write_caption_files(self, segments, out_dir, stem, **kw):  # type: ignore[no-untyped-def]
        self.calls.append(kw)
        p = Path(out_dir) / f"{stem}.ass"
        p.write_text("[Script Info]", encoding="utf-8")
        return {"ass_path": str(p)}


class RenderSequenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "session.mp4"
        self.src.write_bytes(b"x")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make(self, **kw) -> tuple[VideoEngine, FakeComposer, FakeTts, FakeCaptions]:  # type: ignore[no-untyped-def]
        comp, tts, cap = kw.pop("composer", FakeComposer()), kw.pop("tts", FakeTts()), FakeCaptions()
        eng = VideoEngine(workspace_root=self.dir, ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", composer=comp, tts=tts,
                          captions=cap, **kw)
        return eng, comp, tts, cap

    def test_short_clips_with_viral_subtitles(self) -> None:
        eng, comp, tts, cap = self.make()
        progress: list[float] = []
        out = eng.render_sequence(self.src, MOMENTS, self.dir / "out", RenderOptions(templates=["viral_subtitles"]),
                                  source_duration_sec=3600, progress=progress.append)
        self.assertTrue(all(r["ok"] for r in out))
        self.assertEqual(progress, [0.5, 1.0])
        self.assertEqual(len(cap.calls), 2)
        self.assertEqual(cap.calls[0]["narration_words"][0]["word"], "hi")
        self.assertEqual(tts.calls[0]["max_chars"], 600)
        self.assertEqual(comp.renders[0]["zoom_factor"], 1.0)
        self.assertIsNotNone(comp.renders[0]["ass_subtitle_path"])
        # Window grown to fit 12 s narration + offset + tail pad.
        self.assertGreaterEqual(comp.renders[0]["end"] - comp.renders[0]["start"], 12.0)
        self.assertEqual(out[0]["output_filename"], "session_01_CAR_CRASH.mp4")
        self.assertEqual([b["beat"] for b in out[0]["narration"]["beats"]], ["CLIMAX"])
        self.assertEqual(out[0]["narration"]["beats"][0]["label"], "Chaos")

    def test_story_mode_long_form_arc(self) -> None:
        eng, comp, tts, cap = self.make(tts=FakeTts(seconds=20.0))
        opts = RenderOptions(templates=["cinematic_hud"], story_mode=True, story_target_sec=45)
        out = eng.render_sequence(self.src, MOMENTS[:1], self.dir / "out", opts, source_duration_sec=3600)
        r = comp.renders[0]
        self.assertAlmostEqual(r["end"] - r["start"], 45.0, delta=0.01)
        self.assertEqual(r["zoom_factor"], 1.35)
        self.assertIsNone(r["ass_subtitle_path"])  # captions template off
        self.assertEqual(cap.calls, [])
        self.assertEqual(tts.calls[0]["max_chars"], 1200)
        labels = [b["label"] for b in out[0]["narration"]["beats"]]
        self.assertEqual(labels, ["Hook", "Chaos", "Payoff", "Comment Bait"])

    def test_story_target_clamped(self) -> None:
        eng, comp, _, _ = self.make(tts=FakeTts(seconds=5.0))
        eng.render_sequence(self.src, MOMENTS[:1], self.dir / "o", RenderOptions(story_mode=True, story_target_sec=500), 3600)
        self.assertLessEqual(comp.renders[0]["end"] - comp.renders[0]["start"], 60.0 + 1e-6)

    def test_custom_script_split_into_beats(self) -> None:
        eng, _, tts, _ = self.make()
        script = "Bro thought he could outrun five stars.\nThen the bridge happened.\nThe cop just sat there.\nWould you have jumped?"
        out = eng.render_sequence(self.src, MOMENTS[:1], self.dir / "o", RenderOptions(story_mode=True, custom_script=script), 3600)
        self.assertEqual(out[0]["narration"]["source"], "custom_script")
        self.assertIn("bridge", tts.calls[0]["text"])
        self.assertEqual(out[0]["narration"]["beats"][1]["label"], "Chaos")

    def test_failed_moment_isolated(self) -> None:
        eng, comp, _, _ = self.make(composer=FakeComposer(fail_on=0))
        out = eng.render_sequence(self.src, MOMENTS, self.dir / "o", RenderOptions(), 3600)
        self.assertEqual([r["ok"] for r in out], [False, True])
        self.assertIn("nvenc out of memory", out[0]["error"])

    def test_tts_failure_still_renders_without_voice(self) -> None:
        eng, comp, _, _ = self.make(tts=FakeTts(ok=False))
        out = eng.render_sequence(self.src, MOMENTS[:1], self.dir / "o", RenderOptions(), 3600)
        self.assertTrue(out[0]["ok"])
        self.assertIsNone(comp.renders[0]["tts_wav_path"])

    def test_soundboard_pass(self) -> None:
        sfx = self.dir / "07_TEMPLATES" / "sfx"
        sfx.mkdir(parents=True)
        (sfx / "Vine_Boom.wav").write_bytes(b"RIFF")
        ran: list[list[str]] = []

        def runner(cmd: list[str]) -> int:
            ran.append(cmd)
            Path(cmd[-1]).write_bytes(b"mixed")
            return 0

        eng, _, _, _ = self.make(runner=runner)
        out = eng.render_sequence(self.src, MOMENTS[:1], self.dir / "o", RenderOptions(templates=["meme_soundboard"]), 3600)
        self.assertTrue(out[0]["soundboard"]["applied"])
        self.assertEqual(out[0]["soundboard"]["sfx"], "Vine_Boom.wav")
        self.assertEqual(Path(out[0]["output_path"]).read_bytes(), b"mixed")
        self.assertIn("-c:v", ran[0])

    def test_soundboard_without_sfx_reports_reason(self) -> None:
        eng, _, _, _ = self.make()
        out = eng.render_sequence(self.src, MOMENTS[:1], self.dir / "o", RenderOptions(templates=["meme_soundboard"]), 3600)
        self.assertFalse(out[0]["soundboard"]["applied"])

    def test_unknown_template_ignored(self) -> None:
        eng, comp, _, _ = self.make()
        out = eng.render_sequence(self.src, MOMENTS[:1], self.dir / "o", RenderOptions(templates=["nope"]), 3600)
        self.assertEqual(out[0]["templates"], [])

    def test_voices(self) -> None:
        eng, _, _, _ = self.make()
        self.assertEqual(eng.voices()[0]["id"], "rvc:Test")


class HelperTests(unittest.TestCase):
    def test_safe_stem(self) -> None:
        self.assertEqual(safe_stem("../../a b?c"), "a_b_c")
        self.assertEqual(safe_stem("***"), "clip")

    def test_soundboard_cmd_caps_hits(self) -> None:
        hits = [(Path(f"s{i}.wav"), i * 1.5) for i in range(6)]
        cmd = soundboard_mix_cmd("ffmpeg", Path("c.mp4"), hits, Path("o.mp4"))
        self.assertEqual(cmd.count("-i"), 1 + engine_mod.MAX_SOUNDBOARD_HITS)
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("adelay=1500|1500", fc)
        self.assertIn("amix=inputs=4", fc)

    def test_pick_sfx_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "airhorn.mp3").write_bytes(b"x")
            (d / "vine_boom.wav").write_bytes(b"x")
            (d / "notes.txt").write_text("x")
            self.assertEqual(pick_sfx(d, ["CLUTCH_KILL"]).name, "airhorn.mp3")  # type: ignore[union-attr]
            self.assertEqual(pick_sfx(d, ["CAR_CRASH"]).name, "vine_boom.wav")  # type: ignore[union-attr]
            self.assertIsNone(pick_sfx(d, ["REACTION"]))
            self.assertIsNone(pick_sfx(d / "missing", ["CAR_CRASH"]))

    def test_api_provider_list_matches_clipfarm(self) -> None:
        self.assertEqual(app_mod.TTS_PROVIDERS, load_clipfarm("tts").TTS_PROVIDERS)
        self.assertEqual(app_mod.MAX_CUSTOM_SCRIPT_CHARS, load_clipfarm("tts").MAX_CUSTOM_SCRIPT_CHARS)

    def test_story_beats_match_clipfarm(self) -> None:
        self.assertEqual(tuple(engine_mod.STORY_BEAT_LABELS), load_clipfarm("ai_vision_director").STORY_ARC_BEATS)


@unittest.skipUnless(helpers.FFMPEG and helpers.FFPROBE, "ffmpeg/ffprobe not on PATH")
class RealRenderTests(unittest.TestCase):
    def test_real_composer_renders_vertical_clip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            src = helpers.make_crash_video(d / "src.mp4", duration=6.0, crash_at=2.0)
            eng = VideoEngine(workspace_root=d, ffmpeg_bin=helpers.FFMPEG, ffprobe_bin=helpers.FFPROBE,
                              tts=FakeTts(ok=False), captions=FakeCaptions())
            opts = RenderOptions(templates=["cinematic_hud"], prefer_nvenc=False,
                                 export_preset={"preset_id": "test", "width": 360, "height": 640, "fps": 30, "max_duration_sec": 60.0})
            m = [{"start_sec": 0.5, "end_sec": 3.5, "peak_sec": 2.0, "score": 70.0, "tags": ["CAR_CRASH"]}]
            out = eng.render_sequence(src, m, d / "out", opts, source_duration_sec=6.0)
            self.assertTrue(out[0]["ok"], out[0].get("error"))
            r = out[0]["render"]
            self.assertEqual((r["verified_width"], r["verified_height"]), (360, 640))
            self.assertTrue(Path(out[0]["output_path"]).stat().st_size > 1000)


if __name__ == "__main__":
    unittest.main()
