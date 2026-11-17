"""
Long-form ingestion: resumable chunked uploads and the async chunked analysis driver.

Uploads
  Browsers send multi-GB recordings as sequential `PUT` parts (`offset` must equal the bytes
  already stored, so a dropped connection resumes from `received_bytes`). Each part is appended
  straight to `<root>/<upload_id>/source.part` and hashed incrementally; memory per request is
  one part (default cap 16 MB). `complete()` checks the byte count (and SHA-256 when supplied)
  before the file becomes `source<ext>`.

Analysis
  `analyze_long_form()` plans overlapping chunks (`chunking.plan_chunks`) and analyses them one
  at a time in a worker thread (`asyncio.to_thread`), so the event loop stays free and only one
  FFmpeg decode pair is alive per job: RAM/VRAM stay flat for a 6 hour session.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
from typing import Any, Awaitable, Callable

from . import activity
from .chunking import DEFAULT_CHUNK_SEC, DEFAULT_OVERLAP_SEC, owned_moments, plan_chunks, select_top_moments

ALLOWED_EXTENSIONS = (".mp4", ".mkv", ".mov", ".webm")
DEFAULT_MAX_UPLOAD_BYTES = 64 * 1024**3  # 64 GB: about 6 h of 1080p60 OBS footage
DEFAULT_MAX_PART_BYTES = 16 * 1024**2
UPLOAD_ID_RE = re.compile(r"^[a-f0-9]{32}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9 ._-]+")


class UploadError(ValueError):
    """Client-side upload error; `status` maps to the HTTP status code."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class UploadRecord:
    upload_id: str
    owner_id: str
    filename: str
    extension: str
    total_bytes: int
    received_bytes: int = 0
    status: str = "uploading"  # uploading | complete
    sha256: str | None = None
    created_at: float = 0.0
    path: str | None = None


def clean_filename(name: Any) -> tuple[str, str]:
    """Display-safe base name and lower-case extension; raises for a non-video extension."""
    raw = Path(str(name or "")).name  # strips any client-side directory, incl. "..\\"
    ext = Path(raw).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise UploadError(f"unsupported file type {ext or '(none)'}; allowed: {', '.join(ALLOWED_EXTENSIONS)}", 415)
    stem = FILENAME_SAFE_RE.sub("_", Path(raw).stem).strip(" ._")[:120] or "recording"
    return stem + ext, ext


class UploadStore:
    """Disk-backed resumable upload store. Thread-safe; one lock per upload id."""

    def __init__(self, root: Path, max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES, max_part_bytes: int = DEFAULT_MAX_PART_BYTES) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_upload_bytes = int(max_upload_bytes)
        self.max_part_bytes = int(max_part_bytes)
        self._locks: dict[str, threading.Lock] = {}
        self._hashers: dict[str, Any] = {}
        self._guard = threading.Lock()

    def _lock(self, upload_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(upload_id, threading.Lock())

    def _dir(self, upload_id: str) -> Path:
        if not UPLOAD_ID_RE.match(str(upload_id)):
            raise UploadError("invalid upload id", 404)
        return self.root / upload_id

    def _meta_path(self, upload_id: str) -> Path:
        return self._dir(upload_id) / "upload.json"

    def _save(self, rec: UploadRecord) -> None:
        tmp = self._meta_path(rec.upload_id).with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(rec)), encoding="utf-8")
        tmp.replace(self._meta_path(rec.upload_id))

    def get(self, upload_id: str, owner_id: str | None = None) -> UploadRecord:
        meta = self._meta_path(upload_id)
        if not meta.exists():
            raise UploadError("upload not found", 404)
        rec = UploadRecord(**json.loads(meta.read_text(encoding="utf-8")))
        if owner_id is not None and rec.owner_id != owner_id:
            raise UploadError("upload not found", 404)  # never reveal other tenants' ids
        return rec

    def create(self, owner_id: str, filename: str, total_bytes: int) -> UploadRecord:
        name, ext = clean_filename(filename)
        if not isinstance(total_bytes, int) or isinstance(total_bytes, bool) or total_bytes <= 0:
            raise UploadError("total_bytes must be a positive integer")
        if total_bytes > self.max_upload_bytes:
            raise UploadError(f"file exceeds the {self.max_upload_bytes // 1024**3} GB upload limit", 413)
        upload_id = secrets.token_hex(16)
        self._dir(upload_id).mkdir(parents=True)
        (self._dir(upload_id) / "source.part").touch()
        rec = UploadRecord(upload_id, owner_id, name, ext, total_bytes, created_at=time.time())
        self._save(rec)
        return rec

    def _hasher(self, rec: UploadRecord) -> Any:
        h = self._hashers.get(rec.upload_id)
        if h is None:
            # Process restarted mid-upload: rebuild the running hash from disk once.
            h = hashlib.sha256()
            with open(self._dir(rec.upload_id) / "source.part", "rb") as fh:
                for block in iter(lambda: fh.read(8 * 1024**2), b""):
                    h.update(block)
            self._hashers[rec.upload_id] = h
        return h

    def append(self, upload_id: str, owner_id: str, offset: int, data: bytes) -> UploadRecord:
        if len(data) == 0:
            raise UploadError("empty part")
        if len(data) > self.max_part_bytes:
            raise UploadError(f"part exceeds {self.max_part_bytes} bytes", 413)
        with self._lock(upload_id):
            rec = self.get(upload_id, owner_id)
            if rec.status != "uploading":
                raise UploadError("upload already completed", 409)
            if offset != rec.received_bytes:
                raise UploadError(f"offset mismatch: expected {rec.received_bytes}", 409)
            if rec.received_bytes + len(data) > rec.total_bytes:
                raise UploadError("part runs past total_bytes", 413)
            h = self._hasher(rec)
            with open(self._dir(upload_id) / "source.part", "ab") as fh:
                fh.write(data)
            h.update(data)
            rec.received_bytes += len(data)
            self._save(rec)
            return rec

    def complete(self, upload_id: str, owner_id: str, sha256: str | None = None) -> UploadRecord:
        with self._lock(upload_id):
            rec = self.get(upload_id, owner_id)
            if rec.status == "complete":
                return rec
            if rec.received_bytes != rec.total_bytes:
                raise UploadError(f"incomplete upload: {rec.received_bytes}/{rec.total_bytes} bytes", 409)
            digest = self._hasher(rec).hexdigest()
            if sha256 is not None:
                if not SHA256_RE.match(sha256.lower()):
                    raise UploadError("sha256 must be 64 hex characters")
                if sha256.lower() != digest:
                    raise UploadError("sha256 mismatch", 422)
            final = self._dir(upload_id) / f"source{rec.extension}"
            (self._dir(upload_id) / "source.part").replace(final)
            rec.status, rec.sha256, rec.path = "complete", digest, str(final)
            self._hashers.pop(upload_id, None)
            self._save(rec)
            return rec


# ------------------------------------------------------------------------------
# Probe + async chunked analysis
# ------------------------------------------------------------------------------

def probe_duration(ffprobe_bin: str, source: Path) -> float:
    res = subprocess.run(
        [ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(source)],
        capture_output=True, text=True, check=False, timeout=60,
    )
    try:
        return float(res.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"ffprobe could not read duration of {source.name}") from exc


ChunkAnalyzer = Callable[[str, str, Any, activity.ActivityConfig], list[dict[str, Any]]]
Progress = Callable[[float, str], Awaitable[None] | None]


async def analyze_long_form(
    source: Path,
    duration_sec: float,
    ffmpeg_bin: str = "ffmpeg",
    chunk_sec: float = DEFAULT_CHUNK_SEC,
    overlap_sec: float = DEFAULT_OVERLAP_SEC,
    cfg: activity.ActivityConfig | None = None,
    max_moments: int = 12,
    min_gap_sec: float = 20.0,
    analyzer: ChunkAnalyzer | None = None,
    progress: Progress | None = None,
    cancel: asyncio.Event | None = None,
) -> dict[str, Any]:
    """
    Sliding-window analysis of a whole recording. Returns `moments` (top `max_moments`, time order),
    `all_moments` count, `chunks` count and per-tag counts. Raises `asyncio.CancelledError` when
    `cancel` is set between chunks.
    """
    cfg = cfg or activity.ActivityConfig()
    run = analyzer or (lambda ff, src, ch, c: activity.analyze_chunk(ff, src, ch, c))
    chunks = plan_chunks(duration_sec, chunk_sec, overlap_sec)
    found: list[dict[str, Any]] = []
    for ch in chunks:
        if cancel is not None and cancel.is_set():
            raise asyncio.CancelledError("analysis cancelled")
        moments = await asyncio.to_thread(run, ffmpeg_bin, str(source), ch, cfg)
        found.extend(owned_moments(ch, moments, final=ch.index == len(chunks) - 1))
        if progress is not None:
            maybe = progress((ch.index + 1) / len(chunks), f"chunk {ch.index + 1}/{len(chunks)}")
            if asyncio.iscoroutine(maybe):
                await maybe
    tag_counts: dict[str, int] = {}
    for m in found:
        for t in m["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    return {
        "moments": select_top_moments(found, max_moments, min_gap_sec),
        "all_moments": len(found),
        "chunks": len(chunks),
        "tag_counts": tag_counts,
        "duration_sec": round(float(duration_sec), 3),
    }
