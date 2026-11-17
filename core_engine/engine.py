"""
`VideoEngine`: the GTA6-Clipper facade over the ZorvexClipFarm Layer 1 video factory.

Nothing is copied out of `zorvex_clip_farm`; this adapter imports it (repository root on
`sys.path`) and drives it with a sequence plan. That keeps one renderer, one TTS/RVC stack and
one vision director for the whole studio, with the clip farm's own test suite still guarding it.

  * `composer.render_vertical_clip`   9:16 MP4, h264_nvenc with automatic libx264 fallback
  * `tts.synthesize_tts`              Kokoro / SAPI / Edge / ElevenLabs / warm RVC character voices
  * `ai_vision_director.story_arc_script`  Hook -> Chaos (climax) -> Payoff -> Comment Bait narration
  * `captions.write_caption_files`    animated kinetic ASS subtitles (TikTok Viral Subtitles template)

Every collaborator is injectable, so tests render a full sequence without FFmpeg or a GPU.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib
from pathlib import Path
import re
import shutil
import sys
from types import ModuleType
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]

TEMPLATES: dict[str, dict[str, Any]] = {
    "cinematic_hud": {
        "label": "GTA V/VI Cinematic HUD",
        "crop_mode": "center_action",
        "zoom_factor": 1.35,  # tighter action crop: keeps the car / player centre frame, minimap off-frame
    },
    "viral_subtitles": {"label": "TikTok Viral Subtitles", "kinetic_captions": True},
    "meme_soundboard": {"label": "Meme Soundboard", "soundboard": True},
}
STORY_BEAT_LABELS = {"HOOK": "Hook", "CLIMAX": "Chaos", "TROLL_PAYOFF": "Payoff", "COMMENT_BAIT": "Comment Bait"}
SFX_DIR = Path("07_TEMPLATES") / "sfx"
SFX_EXTENSIONS = (".wav", ".mp3", ".ogg")
SFX_BY_TAG = {  # file stem looked up in 07_TEMPLATES/sfx/, first match wins
    "CAR_CRASH": ("crash", "bruh", "vine_boom"),
    "CLUTCH_KILL": ("airhorn", "mlg", "vine_boom"),
    "GUNFIGHT": ("vine_boom", "airhorn"),
    "POLICE_CHASE": ("siren", "run"),
    "REACTION": ("laugh", "bruh"),
}
MAX_SOUNDBOARD_HITS = 3
LONG_FORM_TARGETS = (30.0, 60.0)
SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _ensure_repo_on_path() -> None:
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def load_clipfarm(name: str) -> ModuleType:
    """Import `zorvex_clip_farm.<name>` from the monorepo root."""
    _ensure_repo_on_path()
    return importlib.import_module(f"zorvex_clip_farm.{name}")


@dataclass
class RenderOptions:
    templates: Sequence[str] = ("viral_subtitles",)
    story_mode: bool = False
    story_target_sec: float = 45.0
    custom_script: str | None = None
    tts_provider: str = "offline_local"
    voice: str | None = None
    rvc_pitch: int | None = None
    language: str = "EN"
    tone: str | None = None
    export_preset: dict[str, Any] = field(
        default_factory=lambda: {"preset_id": "tiktok", "width": 1080, "height": 1920, "fps": 60, "max_duration_sec": 60.0}
    )
    prefer_nvenc: bool = True


def safe_stem(text: str, fallback: str = "clip") -> str:
    out = SAFE_STEM_RE.sub("_", str(text or "")).strip("_")[:60]
    return out or fallback


def pick_sfx(sfx_dir: Path, tags: Sequence[str]) -> Path | None:
    """First soundboard file whose stem matches a tag's preference list (`07_TEMPLATES/sfx/`)."""
    if not sfx_dir.is_dir():
        return None
    files = {p.stem.lower(): p for p in sfx_dir.iterdir() if p.is_file() and p.suffix.lower() in SFX_EXTENSIONS}
    for tag in tags:
        for stem in SFX_BY_TAG.get(tag, ()):
            if stem in files:
                return files[stem]
    return None


def soundboard_mix_cmd(ffmpeg_bin: str, clip: Path, hits: Sequence[tuple[Path, float]], out: Path) -> list[str]:
    """
    FFmpeg command that overlays meme SFX `hits` ((file, offset_sec) pairs) on `clip` without
    re-encoding video. At most `MAX_SOUNDBOARD_HITS`; each hit is delayed to its offset and
    mixed at 0.9 under the existing track.
    """
    hits = list(hits)[:MAX_SOUNDBOARD_HITS]
    cmd = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error", "-i", str(clip)]
    for path, _ in hits:
        cmd += ["-i", str(path)]
    parts = []
    labels = ["[0:a]"]
    for i, (_, offset) in enumerate(hits, start=1):
        ms = max(0, round(float(offset) * 1000))
        parts.append(f"[{i}:a]adelay={ms}|{ms},volume=0.9[s{i}]")
        labels.append(f"[s{i}]")
    parts.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:normalize=0[aout]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "0:v", "-map", "[aout]", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    return cmd


class VideoEngine:
    """Sequence renderer. Pass fakes for `composer` / `tts` / `director` / `captions` in tests."""

    def __init__(
        self,
        workspace_root: Path | None = None,
        ffmpeg_bin: str | None = None,
        ffprobe_bin: str | None = None,
        composer: Any = None,
        tts: Any = None,
        director: Any = None,
        captions: Any = None,
        runner: Any = None,
    ) -> None:
        self.workspace_root = Path(workspace_root or REPO_ROOT)
        self.ffmpeg_bin = ffmpeg_bin or shutil.which("ffmpeg") or "ffmpeg"
        self.ffprobe_bin = ffprobe_bin or shutil.which("ffprobe") or "ffprobe"
        self._composer, self._tts, self._director, self._captions = composer, tts, director, captions
        self._runner = runner

    # Lazy Layer 1 imports: importing the engine never pulls torch / RVC / FFmpeg probes.
    @property
    def composer(self) -> Any:
        if self._composer is None:
            self._composer = load_clipfarm("composer")
        return self._composer

    @property
    def tts(self) -> Any:
        if self._tts is None:
            self._tts = load_clipfarm("tts")
        return self._tts

    @property
    def director(self) -> Any:
        if self._director is None:
            self._director = load_clipfarm("ai_vision_director")
        return self._director

    @property
    def captions(self) -> Any:
        if self._captions is None:
            self._captions = load_clipfarm("captions")
        return self._captions

    def _run(self, cmd: list[str]) -> int:
        if self._runner is not None:
            return int(self._runner(cmd))
        import subprocess

        return subprocess.run(cmd, capture_output=True, check=False, timeout=300).returncode

    def voices(self) -> list[dict[str, Any]]:
        """RVC character voices discovered under `07_TEMPLATES/voices/custom/`."""
        try:
            return list(self.tts.discover_rvc_voices(self.workspace_root))
        except Exception:  # noqa: BLE001 - voice discovery must never break the API
            return []

    def narration(self, moment: dict[str, Any], opts: RenderOptions, seed: str) -> dict[str, Any]:
        """Story-arc narration (custom script wins), with beats relabelled Hook/Chaos/Payoff/Comment Bait."""
        max_chars = 1200 if opts.story_mode else 600
        res = self.director.story_arc_script(
            list(moment.get("tags", [])), language=opts.language, seed=seed,
            custom_script=opts.custom_script, tone=opts.tone, max_chars=max_chars,
        )
        if not opts.story_mode and not opts.custom_script:
            # Short clip: the climax line alone carries a 6-12 s highlight.
            climax = next((b["text"] for b in res.get("beats", []) if b.get("beat") == "CLIMAX"), None)
            if climax:
                res = {**res, "text": climax, "beats": [{"beat": "CLIMAX", "text": climax}]}
        res["beats"] = [{**b, "label": STORY_BEAT_LABELS.get(b.get("beat", ""), b.get("beat", ""))} for b in res.get("beats", [])]
        return res

    def render_sequence(
        self,
        source: Path,
        moments: Sequence[dict[str, Any]],
        output_dir: Path,
        opts: RenderOptions | None = None,
        source_duration_sec: float | None = None,
        progress: Any = None,
    ) -> list[dict[str, Any]]:
        """
        Render one 9:16 export per moment: narration -> TTS -> window fit -> captions -> render
        -> optional soundboard pass. A failed moment is reported (`ok: False`) and never stops the batch.
        """
        opts = opts or RenderOptions()
        output_dir.mkdir(parents=True, exist_ok=True)
        enabled = [t for t in opts.templates if t in TEMPLATES]
        style: dict[str, Any] = {}
        for t in enabled:
            style.update(TEMPLATES[t])
        results: list[dict[str, Any]] = []
        total = max(1, len(moments))
        for n, moment in enumerate(moments):
            stem = safe_stem(f"{source.stem}_{n + 1:02d}_{'_'.join(moment.get('tags', [])) or 'moment'}")
            try:
                results.append(self._render_one(source, moment, output_dir, stem, opts, style, enabled, source_duration_sec))
            except Exception as exc:  # noqa: BLE001 - isolate per-clip failures
                results.append({"ok": False, "stem": stem, "moment": moment, "error": str(exc)[:300]})
            if progress is not None:
                progress((n + 1) / total)
        return results

    def _render_one(
        self, source: Path, moment: dict[str, Any], out_dir: Path, stem: str, opts: RenderOptions,
        style: dict[str, Any], enabled: list[str], source_duration_sec: float | None,
    ) -> dict[str, Any]:
        comp = self.composer
        start, end = float(moment["start_sec"]), float(moment["end_sec"])
        min_len = comp.MIN_CLIP_SEC if hasattr(comp, "MIN_CLIP_SEC") else 1.0
        if opts.story_mode:
            target = min(max(float(opts.story_target_sec), LONG_FORM_TARGETS[0]), LONG_FORM_TARGETS[1])
            start, end = comp.long_form_window(start, end, target, source_duration_sec)
            min_len = end - start  # the arc never shrinks below its target, only grows for a longer script
        story = self.narration(moment, opts, seed=stem)
        wav = out_dir / f"{stem}_tts.wav"
        tts_res = self.tts.synthesize_tts(
            story["text"], wav, provider=opts.tts_provider, voice=opts.voice, workspace_root=self.workspace_root,
            ffmpeg_bin=self.ffmpeg_bin, language=opts.language, rvc_pitch=opts.rvc_pitch,
            max_chars=1200 if opts.story_mode else 600,
        )
        narr_sec = tts_res.get("duration_sec") if tts_res.get("synthesized") else None
        max_dur = float(opts.export_preset.get("max_duration_sec", 60.0))
        fit = comp.fit_window_to_narration(start, end, narr_sec, max_duration_sec=max_dur, source_duration_sec=source_duration_sec,
                                           min_duration_sec=min_len)
        start, end = float(fit["start_sec"]), float(fit["end_sec"])
        ass_path = None
        if style.get("kinetic_captions"):
            cap = self.captions.write_caption_files(
                [], out_dir, stem, hook_text=next((b["text"] for b in story["beats"] if b["beat"] == "HOOK"), None),
                clip_duration_sec=end - start, narration_words=tts_res.get("word_timings") or tts_res.get("words"), narration_offset_sec=0.2,
                language=opts.language,
            )
            ass = cap.get("ass_path")
            ass_path = Path(ass) if ass else None
        rendered = comp.render_vertical_clip(
            self.ffmpeg_bin, self.ffprobe_bin, source, out_dir, f"{stem}.mp4", start, end, opts.export_preset,
            crop_mode=style.get("crop_mode", "center_action"), ass_subtitle_path=ass_path,
            tts_wav_path=wav if tts_res.get("synthesized") else None, prefer_hw_encoder=opts.prefer_nvenc,
            zoom_factor=float(style.get("zoom_factor", 1.0)),
        )
        soundboard = None
        if style.get("soundboard"):
            soundboard = self._soundboard(Path(rendered["output_path"]), moment, start)
        return {
            "ok": True, "stem": stem, "moment": moment, "templates": enabled,
            "window": {"start_sec": start, "end_sec": end}, "narration": story, "tts": {k: tts_res.get(k) for k in ("synthesized", "provider", "voice", "duration_sec", "reason")},
            "render": rendered, "soundboard": soundboard,
            "output_path": rendered["output_path"], "output_filename": rendered["output_filename"],
        }

    def _soundboard(self, clip: Path, moment: dict[str, Any], window_start: float) -> dict[str, Any]:
        sfx = pick_sfx(self.workspace_root / SFX_DIR, moment.get("tags", []))
        if sfx is None:
            return {"applied": False, "reason": f"no matching SFX in {SFX_DIR.as_posix()}/"}
        offset = max(0.0, float(moment.get("peak_sec", window_start)) - window_start)
        tmp = clip.with_name(clip.stem + "_sfx" + clip.suffix)
        code = self._run(soundboard_mix_cmd(self.ffmpeg_bin, clip, [(sfx, offset)], tmp))
        if code != 0 or not tmp.exists():
            return {"applied": False, "reason": f"ffmpeg exit {code}"}
        tmp.replace(clip)
        return {"applied": True, "sfx": sfx.name, "offset_sec": round(offset, 3)}
