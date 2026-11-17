"""Shared test setup: puts gta6-clipper/ and the monorepo root on sys.path; synthetic media helpers."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_ROOT.parent
for p in (str(APP_ROOT), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def make_crash_video(path: Path, duration: float = 20.0, crash_at: float = 10.0) -> Path:
    """Black 160x90 clip with a quiet tone; at `crash_at` a 0.6 s white flash plus a loud burst."""
    assert FFMPEG
    enable = f"between(t,{crash_at},{crash_at + 0.6})"
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=black:s=160x90:r=10:d={duration}",
        "-f", "lavfi", "-i", f"sine=f=220:sample_rate=16000:d={duration}",
        "-filter_complex",
        f"[0:v]drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='{enable}'[v];"
        f"[1:a]volume='if({enable},1.0,0.01)':eval=frame[a]",
        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    return path


def make_silent_video(path: Path, duration: float = 6.0) -> Path:
    assert FFMPEG
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"testsrc2=s=320x180:r=30:d={duration}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(path)], check=True, capture_output=True, timeout=120)
    return path
