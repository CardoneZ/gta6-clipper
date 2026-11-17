"""Long-form chunk planning, boundary ownership, top-moment selection and the async chunk driver."""

from __future__ import annotations

import asyncio
import math
import unittest

import helpers  # noqa: F401  (sys.path)
from core_engine import ingestion
from core_engine.chunking import Chunk, owned_moments, plan_chunks, select_top_moments


class PlanChunksTests(unittest.TestCase):
    def test_cores_tile_duration_exactly(self) -> None:
        chunks = plan_chunks(1234.5, chunk_sec=300, overlap_sec=15)
        self.assertEqual(len(chunks), 5)
        self.assertEqual(chunks[0].core_start, 0.0)
        self.assertEqual(chunks[-1].core_end, 1234.5)
        for a, b in zip(chunks, chunks[1:]):
            self.assertEqual(a.core_end, b.core_start)

    def test_overlap_on_both_sides_clamped_to_source(self) -> None:
        chunks = plan_chunks(900, 300, 15)
        self.assertEqual((chunks[0].start, chunks[0].end), (0.0, 315.0))
        self.assertEqual((chunks[1].start, chunks[1].end), (285.0, 615.0))
        self.assertEqual((chunks[2].start, chunks[2].end), (585.0, 900.0))

    def test_six_hour_session_stays_bounded(self) -> None:
        chunks = plan_chunks(6 * 3600, 300, 15)
        self.assertEqual(len(chunks), 72)
        self.assertTrue(all(c.duration <= 330.0 for c in chunks))

    def test_short_source_single_chunk(self) -> None:
        chunks = plan_chunks(42.0)
        self.assertEqual(len(chunks), 1)
        self.assertEqual((chunks[0].start, chunks[0].end), (0.0, 42.0))

    def test_exact_multiple_has_no_empty_tail_chunk(self) -> None:
        self.assertEqual(len(plan_chunks(600, 300, 10)), 2)

    def test_chunk_and_overlap_clamped(self) -> None:
        chunks = plan_chunks(100, chunk_sec=1, overlap_sec=999)
        self.assertEqual(chunks[0].core_end - chunks[0].core_start, 10.0)  # MIN_CHUNK_SEC
        self.assertEqual(chunks[1].start, 5.0)  # overlap capped at half a core

    def test_rejects_bad_durations(self) -> None:
        for bad in (0, -5, math.nan, math.inf, "x", None, 25 * 3600):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                plan_chunks(bad)  # type: ignore[arg-type]

    def test_rejects_non_finite_chunk(self) -> None:
        with self.assertRaises(ValueError):
            plan_chunks(100, chunk_sec=math.nan)


class OwnershipTests(unittest.TestCase):
    def test_boundary_moment_owned_by_exactly_one_chunk(self) -> None:
        chunks = plan_chunks(600, 300, 15)
        moment = {"start_sec": 295.0, "end_sec": 305.0, "peak_sec": 300.0, "score": 70}
        owners = [c.index for c in chunks if owned_moments(c, [moment], final=c.index == len(chunks) - 1)]
        self.assertEqual(owners, [1])

    def test_final_chunk_owns_last_instant(self) -> None:
        c = Chunk(0, 0.0, 10.0, 0.0, 10.0)
        self.assertTrue(c.owns(10.0, final=True))
        self.assertFalse(c.owns(10.0, final=False))


class SelectTopTests(unittest.TestCase):
    def test_top_by_score_with_gap_returned_in_time_order(self) -> None:
        ms = [
            {"start_sec": 100, "end_sec": 110, "score": 90},
            {"start_sec": 112, "end_sec": 120, "score": 80},  # within 20 s gap of the 90 -> dropped
            {"start_sec": 10, "end_sec": 20, "score": 50},
            {"start_sec": 500, "end_sec": 510, "score": 70},
        ]
        out = select_top_moments(ms, limit=3, min_gap_sec=20)
        self.assertEqual([m["start_sec"] for m in out], [10, 100, 500])

    def test_limit(self) -> None:
        ms = [{"start_sec": i * 100, "end_sec": i * 100 + 5, "score": i} for i in range(10)]
        self.assertEqual(len(select_top_moments(ms, limit=4)), 4)
        self.assertEqual(select_top_moments(ms, limit=0), [])


class AnalyzeLongFormTests(unittest.TestCase):
    def test_overlap_duplicates_removed_and_progress_reported(self) -> None:
        calls: list[tuple[float, float]] = []

        def fake(ff: str, src: str, ch: Chunk, cfg: object) -> list[dict]:
            calls.append((ch.start, ch.end))
            # Every chunk whose window covers t=300 "sees" the same crash.
            if ch.start <= 300 <= ch.end:
                return [{"start_sec": 296.0, "end_sec": 304.0, "peak_sec": 300.0, "score": 77.0, "tags": ["CAR_CRASH"]}]
            return []

        progress: list[float] = []
        res = asyncio.run(ingestion.analyze_long_form(
            ingestion.Path("x.mp4"), 900, chunk_sec=300, overlap_sec=15, analyzer=fake,
            progress=lambda f, label: progress.append(f),
        ))
        self.assertEqual(len(calls), 3)
        self.assertEqual(res["all_moments"], 1)
        self.assertEqual(res["tag_counts"], {"CAR_CRASH": 1})
        self.assertEqual(progress[-1], 1.0)
        self.assertEqual(len(progress), 3)

    def test_cancel_between_chunks(self) -> None:
        async def go() -> None:
            ev = asyncio.Event()

            def fake(ff: str, src: str, ch: Chunk, cfg: object) -> list[dict]:
                ev.set()
                return []

            await ingestion.analyze_long_form(ingestion.Path("x.mp4"), 3000, chunk_sec=300, analyzer=fake, cancel=ev)

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(go())

    def test_async_progress_callback_awaited(self) -> None:
        seen: list[str] = []

        async def cb(frac: float, label: str) -> None:
            seen.append(label)

        asyncio.run(ingestion.analyze_long_form(ingestion.Path("x.mp4"), 600, chunk_sec=300,
                                                analyzer=lambda *a: [], progress=cb))
        self.assertEqual(seen, ["chunk 1/2", "chunk 2/2"])


if __name__ == "__main__":
    unittest.main()
