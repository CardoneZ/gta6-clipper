"""
GTA6-Clipper core engine
========================

Standalone backend layer of the GTA6-Clipper SaaS. It does not fork the battle-tested
ZorvexClipFarm video factory: `engine.VideoEngine` is an adapter over the Layer 1
`zorvex_clip_farm` modules (`composer` FFmpeg/h264_nvenc renderer, `tts` incl. the warm RVC
worker, `ai_vision_director` Ollama/Qwen copy), so every ClipFarm fix flows through unchanged.

New in this layer (game-agnostic, no lore or balance numbers):
  * `chunking`   sliding-window chunk planner with overlap ownership (no double counted moments)
  * `activity`   bounded-memory audio/motion signal analysis and moment classification
  * `ingestion`  resumable chunked upload store and the async long-form analysis driver
  * `jobs`       asyncio processing queue with queue position and ETA
"""

from __future__ import annotations

__version__ = "0.1.0"
__product__ = "GTA6-Clipper Core Engine"
