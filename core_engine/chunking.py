"""
Sliding-window chunk planner for multi-hour recordings.

A long recording is never decoded whole. It is cut into `chunk_sec` windows that overlap by
`overlap_sec` so a moment that straddles a boundary is still seen in full by one chunk. Each
chunk also owns a disjoint `[core_start, core_end)` span; the core spans tile `[0, duration)`
exactly, and a detected moment is kept only by the chunk whose core contains its peak. That
gives full context at the edges with zero duplicate moments.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable

DEFAULT_CHUNK_SEC = 300.0  # 5 min: about 3000 audio hops at 0.1 s, a few hundred KB of floats
DEFAULT_OVERLAP_SEC = 15.0
MIN_CHUNK_SEC = 10.0
MAX_CHUNK_SEC = 1800.0
MAX_SOURCE_SEC = 24 * 3600.0  # one day of footage is the hard ceiling per upload


@dataclass(frozen=True)
class Chunk:
    index: int
    start: float  # decode window start (includes leading overlap)
    end: float  # decode window end (includes trailing overlap)
    core_start: float  # owned span start, inclusive
    core_end: float  # owned span end, exclusive (inclusive for the final chunk)

    @property
    def duration(self) -> float:
        return self.end - self.start

    def owns(self, t: float, final: bool = False) -> bool:
        if final:
            return self.core_start <= t <= self.core_end
        return self.core_start <= t < self.core_end


def _finite(value: Any, name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(out):
        raise ValueError(f"{name} must be finite")
    return out


def plan_chunks(
    duration_sec: float,
    chunk_sec: float = DEFAULT_CHUNK_SEC,
    overlap_sec: float = DEFAULT_OVERLAP_SEC,
) -> list[Chunk]:
    """
    Plan decode chunks for a `duration_sec` source. `chunk_sec` is the owned core length
    (clamped to 10 s - 30 min); `overlap_sec` is added on both sides (clamped to half a core).
    Raises `ValueError` for a non-finite, non-positive or over-24 h duration.
    """
    dur = _finite(duration_sec, "duration_sec")
    if dur <= 0:
        raise ValueError("duration_sec must be positive")
    if dur > MAX_SOURCE_SEC:
        raise ValueError("duration_sec exceeds the 24 h ceiling")
    core = min(max(_finite(chunk_sec, "chunk_sec"), MIN_CHUNK_SEC), MAX_CHUNK_SEC)
    overlap = min(max(_finite(overlap_sec, "overlap_sec"), 0.0), core / 2.0)
    count = max(1, math.ceil(dur / core - 1e-9))
    chunks: list[Chunk] = []
    for i in range(count):
        core_start = i * core
        core_end = min(dur, (i + 1) * core)
        chunks.append(
            Chunk(
                index=i,
                start=round(max(0.0, core_start - overlap), 3),
                end=round(min(dur, core_end + overlap), 3),
                core_start=round(core_start, 3),
                core_end=round(core_end, 3),
            )
        )
    return chunks


def owned_moments(chunk: Chunk, moments: Iterable[dict[str, Any]], final: bool) -> list[dict[str, Any]]:
    """Keep only the moments whose `peak_sec` falls inside the chunk's owned core span."""
    return [m for m in moments if chunk.owns(float(m.get("peak_sec", m.get("start_sec", 0.0))), final)]


def select_top_moments(
    moments: Iterable[dict[str, Any]],
    limit: int = 12,
    min_gap_sec: float = 20.0,
) -> list[dict[str, Any]]:
    """
    Highest-score moments first, skipping any whose window sits within `min_gap_sec` of an
    already picked one (two clips of the same crash are worse than one). Returned in time order.
    """
    ranked = sorted(moments, key=lambda m: (-float(m.get("score", 0.0)), float(m.get("start_sec", 0.0))))
    picked: list[dict[str, Any]] = []
    for m in ranked:
        if len(picked) >= max(0, int(limit)):
            break
        s, e = float(m["start_sec"]), float(m["end_sec"])
        if any(s < float(p["end_sec"]) + min_gap_sec and e > float(p["start_sec"]) - min_gap_sec for p in picked):
            continue
        picked.append(m)
    return sorted(picked, key=lambda m: float(m["start_sec"]))
