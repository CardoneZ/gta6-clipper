"""
Bounded-memory audio / visual activity detection for long gameplay recordings.

Per chunk, FFmpeg streams two tiny signals through pipes (never a full decode in RAM/VRAM):
  * audio: mono 8 kHz s16le, reduced on the fly to one RMS dBFS value per `hop_sec` hop
  * motion: 64x36 grayscale frames at 4 fps, reduced to one mean-abs frame difference per frame
A 5 minute chunk therefore costs about 3000 floats per signal, whatever the source resolution.

The detectors below are explainable signal heuristics, not a trained classifier. They are tuned
for open-world driving / shooting gameplay and tag candidate windows; the vision director
(`ai_vision_director`, Ollama/Qwen) can re-rank and caption them afterwards.

  CAR_CRASH      sharp loudness onset that coincides with a motion spike
  CLUTCH_KILL    cluster of 3+ sharp onsets (gunfire) that ends in a sudden drop to quiet
  GUNFIGHT       cluster of 3+ sharp onsets without the resolving drop
  POLICE_CHASE   sustained high motion plus sustained elevated loudness (sirens, engines) for 8 s+
  REACTION       loud spike over a calm screen (voice chat, laughter, streamer reaction)
"""

from __future__ import annotations

from dataclasses import dataclass, field
import subprocess
from typing import Any, Callable, Sequence

import numpy as np

from .chunking import Chunk

HOP_SEC = 0.1
AUDIO_RATE = 8000
MOTION_FPS = 4
MOTION_W, MOTION_H = 64, 36
SILENCE_DB = -90.0
READ_BLOCK_HOPS = 50  # pipe read size: 5 s of 8 kHz audio per read() call

MOMENT_TAGS = ("CAR_CRASH", "CLUTCH_KILL", "GUNFIGHT", "POLICE_CHASE", "REACTION")
TAG_BASE_SCORE = {"CAR_CRASH": 62.0, "CLUTCH_KILL": 66.0, "GUNFIGHT": 52.0, "POLICE_CHASE": 58.0, "REACTION": 45.0}


@dataclass(frozen=True)
class ActivityConfig:
    hop_sec: float = HOP_SEC
    rise_db: float = 9.0  # onset: loudness jump over the trailing `rise_window_sec` minimum
    rise_window_sec: float = 0.3
    crash_motion_z: float = 2.0
    crash_pad_sec: tuple[float, float] = (4.0, 4.0)
    burst_count: int = 3
    burst_window_sec: float = 4.0
    clutch_quiet_window_sec: float = 3.0
    chase_motion_z: float = 1.0
    chase_audio_z: float = 0.5
    chase_min_sec: float = 8.0
    chase_gap_sec: float = 1.0
    chase_max_sec: float = 30.0
    reaction_audio_z: float = 2.5
    reaction_calm_motion_z: float = 0.5
    max_moment_sec: float = 40.0


@dataclass
class Signals:
    """Hop-aligned signals of one chunk. `t0` is the absolute source time of hop 0."""

    t0: float
    hop_sec: float
    audio_db: np.ndarray
    motion: np.ndarray
    meta: dict[str, Any] = field(default_factory=dict)

    def time(self, i: int) -> float:
        return self.t0 + (i + 0.5) * self.hop_sec


# ------------------------------------------------------------------------------
# Signal math (pure, unit tested)
# ------------------------------------------------------------------------------

def hop_rms_db(samples: np.ndarray, hop_samples: int) -> np.ndarray:
    """RMS dBFS per full hop of int16 `samples` (a trailing partial hop is dropped)."""
    n = len(samples) // hop_samples
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    x = samples[: n * hop_samples].astype(np.float32).reshape(n, hop_samples) / 32768.0
    rms = np.sqrt(np.mean(x * x, axis=1))
    return np.maximum(20.0 * np.log10(np.maximum(rms, 1e-12)), SILENCE_DB).astype(np.float32)


def robust_z(x: np.ndarray, floor: float) -> np.ndarray:
    """(x - median) / max(1.4826 * MAD, floor): outlier-proof standard score."""
    if x.size == 0:
        return x.astype(np.float32)
    med = float(np.median(x))
    mad = float(np.median(np.abs(x - med))) * 1.4826
    return ((x - med) / max(mad, floor)).astype(np.float32)


def onsets(audio_db: np.ndarray, rise_db: float, window_hops: int) -> np.ndarray:
    """Hop indices where loudness jumps `rise_db`+ over the minimum of the previous `window_hops`."""
    if audio_db.size <= window_hops:
        return np.zeros(0, dtype=np.int64)
    w = max(1, int(window_hops))
    trailing_min = np.full(audio_db.shape, np.inf, dtype=np.float32)
    for k in range(1, w + 1):
        trailing_min[k:] = np.minimum(trailing_min[k:], audio_db[:-k])
    rise = audio_db - trailing_min
    hits = np.flatnonzero(rise >= rise_db)
    if hits.size == 0:
        return hits
    # Collapse a rising edge that stays above threshold for several hops into its first hop.
    keep = np.concatenate(([True], np.diff(hits) > w))
    return hits[keep]


def resample_motion(motion: np.ndarray, motion_fps: float, hop_sec: float, hops: int) -> np.ndarray:
    """Linear-interpolate per-frame motion onto the audio hop grid (`hops` samples)."""
    if hops <= 0:
        return np.zeros(0, dtype=np.float32)
    if motion.size == 0:
        return np.zeros(hops, dtype=np.float32)
    frame_t = (np.arange(motion.size) + 1.0) / motion_fps  # diff k compares frames k and k+1
    hop_t = (np.arange(hops) + 0.5) * hop_sec
    return np.interp(hop_t, frame_t, motion).astype(np.float32)


def _runs(mask: np.ndarray, max_gap: int) -> list[tuple[int, int]]:
    """[start, end) index runs of True, bridging False gaps of at most `max_gap` hops."""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    runs: list[tuple[int, int]] = []
    s = prev = int(idx[0])
    for i in idx[1:]:
        i = int(i)
        if i - prev > max_gap + 1:
            runs.append((s, prev + 1))
            s = i
        prev = i
    runs.append((s, prev + 1))
    return runs


def _moment(sig: Signals, tag: str, start: float, end: float, peak: float, intensity: float, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    score = min(100.0, TAG_BASE_SCORE[tag] + 6.0 * max(0.0, intensity))
    lo = sig.t0
    hi = sig.t0 + sig.audio_db.size * sig.hop_sec
    out = {
        "start_sec": round(max(lo, start), 3),
        "end_sec": round(min(hi, end), 3),
        "peak_sec": round(peak, 3),
        "score": round(score, 1),
        "tags": [tag],
        "signal": {"intensity": round(float(intensity), 2)},
    }
    if extra:
        out["signal"].update(extra)
    return out


# ------------------------------------------------------------------------------
# Detectors
# ------------------------------------------------------------------------------

def detect_moments(sig: Signals, cfg: ActivityConfig | None = None) -> list[dict[str, Any]]:
    """All tagged moments in `sig`, overlapping detections merged, in time order."""
    cfg = cfg or ActivityConfig()
    hops = sig.audio_db.size
    if hops < 10:
        return []
    hop = sig.hop_sec
    az = robust_z(sig.audio_db, 1.5)
    mz = robust_z(sig.motion if sig.motion.size == hops else np.zeros(hops, dtype=np.float32), 1.0)
    on = onsets(sig.audio_db, cfg.rise_db, max(1, round(cfg.rise_window_sec / hop)))
    found: list[dict[str, Any]] = []
    near = max(1, round(0.5 / hop))

    # CAR_CRASH: onset with a motion spike within +-0.5 s.
    for i in on:
        lo, hi = max(0, i - near), min(hops, i + near + 1)
        mpk = float(np.max(mz[lo:hi]))
        if mpk >= cfg.crash_motion_z:
            t = sig.time(int(i))
            found.append(_moment(sig, "CAR_CRASH", t - cfg.crash_pad_sec[0], t + cfg.crash_pad_sec[1], t,
                                 (mpk - cfg.crash_motion_z) + float(az[i]) / 2.0, {"motion_z": round(mpk, 2)}))

    # GUNFIGHT / CLUTCH_KILL: 3+ onsets inside a 4 s window.
    burst_hops = round(cfg.burst_window_sec / hop)
    quiet_hops = round(cfg.clutch_quiet_window_sec / hop)
    used = -1
    for k in range(len(on)):
        if on[k] <= used:
            continue
        j = k
        while j + 1 < len(on) and on[j + 1] - on[k] <= burst_hops:
            j += 1
        count = j - k + 1
        if count < cfg.burst_count:
            continue
        first, last = int(on[k]), int(on[j])
        # Extend through follow-up shots that keep chaining within the window.
        while j + 1 < len(on) and on[j + 1] - last <= burst_hops:
            j += 1
            last = int(on[j])
        count = j - k + 1
        used = last
        after = az[last + 1: min(hops, last + 1 + quiet_hops)]
        clutch = after.size > 0 and float(np.min(after)) < 0.0 and float(np.max(az[first: last + 1])) > 1.0
        tag = "CLUTCH_KILL" if clutch else "GUNFIGHT"
        t_first, t_last = sig.time(first), sig.time(last)
        found.append(_moment(sig, tag, t_first - 3.0, t_last + (4.0 if clutch else 2.5), t_last,
                             count / cfg.burst_count + float(np.max(az[first: last + 1])) / 3.0,
                             {"shots": count}))

    # POLICE_CHASE: sustained motion + loudness.
    chase_mask = (mz >= cfg.chase_motion_z) & (az >= cfg.chase_audio_z)
    for s, e in _runs(chase_mask, round(cfg.chase_gap_sec / hop)):
        if (e - s) * hop < cfg.chase_min_sec:
            continue
        combined = mz[s:e] + az[s:e]
        pk = s + int(np.argmax(combined))
        if (e - s) * hop > cfg.chase_max_sec:
            half = round(cfg.chase_max_sec / hop / 2)
            s, e = max(s, pk - half), min(e, pk + half)
        found.append(_moment(sig, "POLICE_CHASE", sig.time(s) - 1.0, sig.time(e - 1) + 1.0, sig.time(pk),
                             float(np.mean(combined)) - (cfg.chase_motion_z + cfg.chase_audio_z),
                             {"sustained_sec": round((e - s) * hop, 1)}))

    # REACTION: loud spike over a calm picture.
    reaction_mask = (az >= cfg.reaction_audio_z) & (mz < cfg.reaction_calm_motion_z)
    for s, e in _runs(reaction_mask, round(1.0 / hop)):
        pk = s + int(np.argmax(az[s:e]))
        found.append(_moment(sig, "REACTION", sig.time(s) - 3.0, sig.time(e - 1) + 2.0, sig.time(pk),
                             float(az[pk]) - cfg.reaction_audio_z))

    return merge_moments(found, cfg.max_moment_sec)


def merge_moments(moments: Sequence[dict[str, Any]], max_len_sec: float = 40.0) -> list[dict[str, Any]]:
    """Merge overlapping windows: union span (capped around the best peak), union tags, best score."""
    ordered = sorted(moments, key=lambda m: m["start_sec"])
    merged: list[dict[str, Any]] = []
    for m in ordered:
        if merged and m["start_sec"] <= merged[-1]["end_sec"]:
            cur = merged[-1]
            if m["score"] > cur["score"]:
                cur["peak_sec"] = m["peak_sec"]
                cur["signal"] = {**cur["signal"], **m["signal"]}
            else:
                cur["signal"] = {**m["signal"], **cur["signal"]}
            cur["score"] = round(min(100.0, max(cur["score"], m["score"]) + 3.0), 1)  # combo bonus
            cur["end_sec"] = max(cur["end_sec"], m["end_sec"])
            cur["tags"] = sorted(set(cur["tags"]) | set(m["tags"]), key=MOMENT_TAGS.index)
        else:
            merged.append({**m, "tags": list(m["tags"]), "signal": dict(m["signal"])})
    for m in merged:
        if m["end_sec"] - m["start_sec"] > max_len_sec:
            half = max_len_sec / 2.0
            s = max(m["start_sec"], m["peak_sec"] - half)
            m["start_sec"], m["end_sec"] = round(s, 3), round(min(m["end_sec"], s + max_len_sec), 3)
    return merged


# ------------------------------------------------------------------------------
# FFmpeg streaming readers
# ------------------------------------------------------------------------------

Reader = Callable[[str, str, float, float], np.ndarray]


def _popen(cmd: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)


def read_audio_db(ffmpeg_bin: str, source: str, start: float, duration: float, hop_sec: float = HOP_SEC) -> np.ndarray:
    """Stream mono 8 kHz PCM of `[start, start+duration]` and reduce it to hop dBFS. No audio = silence."""
    hop_samples = max(1, round(AUDIO_RATE * hop_sec))
    cmd = [ffmpeg_bin, "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
           "-i", source, "-vn", "-ac", "1", "-ar", str(AUDIO_RATE), "-f", "s16le", "-"]
    proc = _popen(cmd)
    out: list[np.ndarray] = []
    carry = np.zeros(0, dtype=np.int16)
    block = hop_samples * READ_BLOCK_HOPS * 2
    assert proc.stdout is not None
    try:
        while True:
            data = proc.stdout.read(block)
            if not data:
                break
            pcm = np.frombuffer(data[: len(data) - (len(data) % 2)], dtype="<i2")
            pcm = np.concatenate((carry, pcm)) if carry.size else pcm
            full = (len(pcm) // hop_samples) * hop_samples
            out.append(hop_rms_db(pcm[:full], hop_samples))
            carry = pcm[full:].copy()
    finally:
        proc.stdout.close()
        proc.wait()
    expected = max(0, int(duration / hop_sec))
    got = np.concatenate(out) if out else np.zeros(0, dtype=np.float32)
    if got.size < expected:  # missing / short audio track: pad with silence
        got = np.concatenate((got, np.full(expected - got.size, SILENCE_DB, dtype=np.float32)))
    return got[:expected] if expected else got


def read_motion(ffmpeg_bin: str, source: str, start: float, duration: float, fps: int = MOTION_FPS) -> np.ndarray:
    """Stream 64x36 gray frames at `fps` and return mean-abs differences between consecutive frames."""
    frame_bytes = MOTION_W * MOTION_H
    cmd = [ffmpeg_bin, "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
           "-i", source, "-an", "-vf", f"fps={fps},scale={MOTION_W}:{MOTION_H}:flags=area,format=gray",
           "-f", "rawvideo", "-"]
    proc = _popen(cmd)
    diffs: list[float] = []
    prev: np.ndarray | None = None
    assert proc.stdout is not None
    try:
        while True:
            data = proc.stdout.read(frame_bytes)
            if len(data) < frame_bytes:
                break
            cur = np.frombuffer(data, dtype=np.uint8).astype(np.int16)
            if prev is not None:
                diffs.append(float(np.mean(np.abs(cur - prev))))
            prev = cur
    finally:
        proc.stdout.close()
        proc.wait()
    return np.asarray(diffs, dtype=np.float32)


def analyze_chunk(
    ffmpeg_bin: str,
    source: str,
    chunk: Chunk,
    cfg: ActivityConfig | None = None,
    audio_reader: Reader | None = None,
    motion_reader: Reader | None = None,
) -> list[dict[str, Any]]:
    """Moments detected in one decode window, with absolute source times."""
    cfg = cfg or ActivityConfig()
    audio_fn = audio_reader or (lambda ff, src, s, d: read_audio_db(ff, src, s, d, cfg.hop_sec))
    motion_fn = motion_reader or (lambda ff, src, s, d: read_motion(ff, src, s, d))
    audio_db = audio_fn(ffmpeg_bin, source, chunk.start, chunk.duration)
    motion_raw = motion_fn(ffmpeg_bin, source, chunk.start, chunk.duration)
    motion = resample_motion(motion_raw, MOTION_FPS, cfg.hop_sec, audio_db.size)
    audio_db = np.nan_to_num(audio_db, nan=SILENCE_DB, neginf=SILENCE_DB, posinf=0.0)
    motion = np.nan_to_num(motion, nan=0.0, neginf=0.0, posinf=0.0)
    sig = Signals(t0=chunk.start, hop_sec=cfg.hop_sec, audio_db=audio_db, motion=motion, meta={"chunk": chunk.index})
    return detect_moments(sig, cfg)
