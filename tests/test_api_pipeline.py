"""Chunked uploads, tenant isolation, job queue (position/ETA/cancel), exports library and input validation."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import tempfile
import time
import unittest

import helpers  # noqa: F401
from fastapi.testclient import TestClient

from core_engine.ingestion import UploadError, UploadStore, clean_filename
from core_engine.jobs import JobQueue
from saas.app import Settings, create_app

PAYLOAD = bytes(range(256)) * 400  # 102400 bytes


class UploadStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = UploadStore(Path(self.tmp.name), max_upload_bytes=10**6, max_part_bytes=40_000)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_resumable_roundtrip_and_hash(self) -> None:
        rec = self.store.create("u1", "My Session.MKV", len(PAYLOAD))
        self.assertEqual(rec.filename, "My Session.mkv")
        off = 0
        while off < len(PAYLOAD):
            off = self.store.append(rec.upload_id, "u1", off, PAYLOAD[off:off + 40_000]).received_bytes
        # A fresh store (process restart) rebuilds the running hash from disk.
        fresh = UploadStore(Path(self.tmp.name), max_upload_bytes=10**6, max_part_bytes=40_000)
        done = fresh.complete(rec.upload_id, "u1", hashlib.sha256(PAYLOAD).hexdigest())
        self.assertEqual(done.status, "complete")
        self.assertEqual(Path(done.path or "").read_bytes(), PAYLOAD)
        self.assertEqual(fresh.complete(rec.upload_id, "u1").status, "complete")  # idempotent

    def test_errors(self) -> None:
        rec = self.store.create("u1", "a.mp4", 100)
        cases = [
            (lambda: self.store.append(rec.upload_id, "u1", 5, b"x"), 409),  # offset mismatch
            (lambda: self.store.append(rec.upload_id, "u1", 0, b"x" * 101), 413),  # past total
            (lambda: self.store.append(rec.upload_id, "u1", 0, b""), 400),
            (lambda: self.store.append(rec.upload_id, "u2", 0, b"x"), 404),  # other tenant
            (lambda: self.store.append("../../etc", "u1", 0, b"x"), 404),
            (lambda: self.store.complete(rec.upload_id, "u1"), 409),  # incomplete
            (lambda: self.store.create("u1", "a.exe", 10), 415),
            (lambda: self.store.create("u1", "a.mp4", 10**7), 413),
            (lambda: self.store.create("u1", "a.mp4", 0), 400),
            (lambda: self.store.create("u1", "a.mp4", True), 400),  # type: ignore[arg-type]
        ]
        for fn, status in cases:
            with self.subTest(status=status), self.assertRaises(UploadError) as ctx:
                fn()
            self.assertEqual(ctx.exception.status, status)

    def test_sha_mismatch(self) -> None:
        rec = self.store.create("u1", "a.mp4", 3)
        self.store.append(rec.upload_id, "u1", 0, b"abc")
        with self.assertRaises(UploadError) as ctx:
            self.store.complete(rec.upload_id, "u1", "0" * 64)
        self.assertEqual(ctx.exception.status, 422)

    def test_filename_traversal_stripped(self) -> None:
        self.assertEqual(clean_filename("..\\..\\win<>.mov")[0], "win.mov")
        self.assertEqual(clean_filename("../../<>.mp4")[0], "recording.mp4")
        with self.assertRaises(UploadError):
            clean_filename("../../.mp4")  # dotfile: no real extension


class JobQueueTests(unittest.TestCase):
    def test_position_eta_cancel_failure(self) -> None:
        async def go() -> None:
            gate = asyncio.Event()
            q = JobQueue(concurrency=1)
            q.sec_per_source_min = 10.0

            async def slow(job):  # type: ignore[no-untyped-def]
                await gate.wait()
                return {"ok": True}

            async def boom(job):  # type: ignore[no-untyped-def]
                raise RuntimeError("render exploded")

            await q.start()
            a = q.submit("u", 2.0, slow)
            b = q.submit("u", 3.0, boom)
            c = q.submit("u", 1.0, slow)
            await asyncio.sleep(0.05)
            self.assertEqual(q.position(a.job_id), 0)
            self.assertEqual(q.position(b.job_id), 1)
            self.assertEqual(q.position(c.job_id), 2)
            self.assertEqual(q.eta_sec(a.job_id), 20.0)
            self.assertEqual(q.eta_sec(b.job_id), 50.0)  # 20 running + 30 own
            self.assertEqual(q.eta_sec(c.job_id), 60.0)
            self.assertTrue(q.cancel(c.job_id))
            self.assertEqual(q.jobs[c.job_id].status, "cancelled")
            self.assertIsNone(q.position(c.job_id))
            gate.set()
            for _ in range(100):
                if q.jobs[b.job_id].status in ("done", "failed"):
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(q.jobs[a.job_id].status, "done")
            self.assertEqual(q.jobs[b.job_id].status, "failed")
            self.assertIn("render exploded", q.jobs[b.job_id].error or "")
            self.assertFalse(q.cancel(a.job_id))
            await q.stop()

        asyncio.run(go())


def fake_pipeline_factory(calls: list[dict]):  # type: ignore[no-untyped-def]
    async def fake_pipeline(settings, job, source, options, out_dir):  # type: ignore[no-untyped-def]
        queue = options.pop("_queue")
        calls.append(options)
        await queue.set_progress(job, 0.5, "analyzing chunk 1/1")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "session_01_CAR_CRASH.mp4").write_bytes(b"fake-mp4")
        return {
            "analysis": {"moments": [{"start_sec": 1, "end_sec": 9, "tags": ["CAR_CRASH"], "score": 81}], "chunks": 1},
            "exports": [
                {"ok": True, "output_filename": "session_01_CAR_CRASH.mp4",
                 "moment": {"tags": ["CAR_CRASH"], "score": 81}, "render": {"verified_duration_sec": 8.0},
                 "narration": {"beats": [{"beat": "HOOK", "label": "Hook", "text": "Watch this."}]}},
                {"ok": False, "error": "tts failed"},
            ],
        }
    return fake_pipeline


class ApiPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.calls: list[dict] = []
        settings = Settings(data_root=Path(self.tmp.name), dev_billing=True, max_part_bytes=40_000, auth_rate=(1000, 60.0))
        self.client = TestClient(create_app(settings, pipeline=fake_pipeline_factory(self.calls)))
        self.client.__enter__()
        self.a = self.user("a@example.test")
        self.b = self.user("b@example.test")

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def user(self, email: str) -> dict:
        tok = self.client.post("/api/auth/register", json={"email": email, "password": "longpassword1"}).json()["token"]
        h = {"Authorization": f"Bearer {tok}"}
        self.assertTrue(self.client.post("/api/billing/dev-activate", json={"days": 30}, headers=h).json()["active"])
        return h

    def upload(self, h: dict, data: bytes = PAYLOAD, name: str = "session.mp4") -> str:
        init = self.client.post("/api/uploads", json={"filename": name, "total_bytes": len(data)}, headers=h).json()
        uid, part, off = init["upload_id"], init["part_bytes"], 0
        while off < len(data):
            r = self.client.put(f"/api/uploads/{uid}?offset={off}", content=data[off:off + part], headers=h)
            self.assertEqual(r.status_code, 200, r.text)
            off = r.json()["received_bytes"]
        done = self.client.post(f"/api/uploads/{uid}/complete", json={"sha256": hashlib.sha256(data).hexdigest()}, headers=h)
        self.assertEqual(done.status_code, 200, done.text)
        return uid

    def wait_job(self, h: dict, job_id: str) -> dict:
        for _ in range(200):
            j = self.client.get(f"/api/jobs/{job_id}", headers=h).json()
            if j["status"] in ("done", "failed", "cancelled"):
                return j
            time.sleep(0.02)
        self.fail("job did not finish")

    def test_end_to_end_upload_job_exports(self) -> None:
        uid = self.upload(self.a)
        job = self.client.post("/api/jobs", json={
            "upload_id": uid, "templates": ["cinematic_hud", "viral_subtitles", "viral_subtitles"],
            "story_mode": True, "story_target_sec": 45, "custom_script": "Hook line.\nChaos line.\nPayoff.\nComment?",
            "tts_provider": "rvc_voice", "voice": "rvc:Trevorish", "rvc_pitch": 4, "language": "EN",
        }, headers=self.a)
        self.assertEqual(job.status_code, 200, job.text)
        self.assertIn("queue_position", job.json())
        done = self.wait_job(self.a, job.json()["job_id"])
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(self.calls[0]["templates"], ["cinematic_hud", "viral_subtitles"])  # de-duplicated
        self.assertTrue(self.calls[0]["story_mode"])
        ex = self.client.get("/api/exports", headers=self.a).json()["exports"]
        self.assertEqual(len(ex), 1)  # failed render filtered out
        self.assertEqual(ex[0]["tags"], ["CAR_CRASH"])
        self.assertEqual(ex[0]["beats"][0]["label"], "Hook")
        got = self.client.get(ex[0]["url"], headers=self.a)
        self.assertEqual((got.status_code, got.content), (200, b"fake-mp4"))
        # Tenant isolation: B sees nothing of A.
        self.assertEqual(self.client.get(ex[0]["url"], headers=self.b).status_code, 404)
        self.assertEqual(self.client.get(f"/api/jobs/{done['job_id']}", headers=self.b).status_code, 404)
        self.assertEqual(self.client.get(f"/api/uploads/{uid}", headers=self.b).status_code, 404)
        self.assertEqual(self.client.get("/api/exports", headers=self.b).json()["exports"], [])

    def test_export_path_traversal_blocked(self) -> None:
        uid = self.upload(self.a)
        jid = self.client.post("/api/jobs", json={"upload_id": uid}, headers=self.a).json()["job_id"]
        self.wait_job(self.a, jid)
        for name in ("..%2F..%2Fgtc.sqlite3", "x.exe", "..\\x.mp4"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(f"/api/exports/{jid}/{name}", headers=self.a).status_code, 404)

    def test_job_validation(self) -> None:
        uid = self.upload(self.a)
        bad = [
            {"upload_id": uid, "templates": ["hack"]},
            {"upload_id": uid, "story_target_sec": 120},
            {"upload_id": uid, "story_target_sec": 10},
            {"upload_id": uid, "tts_provider": "shell"},
            {"upload_id": uid, "rvc_pitch": 99},
            {"upload_id": uid, "language": "FR"},
            {"upload_id": uid, "voice": "../../evil"},
            {"upload_id": uid, "custom_script": "x" * 1801},
            {"upload_id": uid, "max_clips": 0},
            {"upload_id": uid, "unexpected": 1},
            {"upload_id": "nothex"},
        ]
        for body in bad:
            with self.subTest(body=list(body)[-1]):
                self.assertEqual(self.client.post("/api/jobs", json=body, headers=self.a).status_code, 422)
        # NaN / Infinity in raw JSON
        for raw in ('{"upload_id":"%s","story_target_sec":NaN}' % uid, '{"upload_id":"%s","story_target_sec":Infinity}' % uid):
            r = self.client.post("/api/jobs", content=raw, headers={**self.a, "Content-Type": "application/json"})
            self.assertEqual(r.status_code, 422)

    def test_incomplete_upload_cannot_be_queued(self) -> None:
        init = self.client.post("/api/uploads", json={"filename": "s.mp4", "total_bytes": 50}, headers=self.a).json()
        r = self.client.post("/api/jobs", json={"upload_id": init["upload_id"]}, headers=self.a)
        self.assertEqual(r.status_code, 409)

    def test_upload_api_errors(self) -> None:
        init = self.client.post("/api/uploads", json={"filename": "s.mp4", "total_bytes": 50_000}, headers=self.a).json()
        uid = init["upload_id"]
        self.assertEqual(self.client.put(f"/api/uploads/{uid}?offset=7", content=b"x", headers=self.a).status_code, 409)
        self.assertEqual(self.client.put(f"/api/uploads/{uid}?offset=-1", content=b"x", headers=self.a).status_code, 422)
        self.assertEqual(self.client.put(f"/api/uploads/{uid}?offset=0", content=b"x" * 40_001, headers=self.a).status_code, 413)
        self.assertEqual(self.client.post("/api/uploads", json={"filename": "s.txt", "total_bytes": 5}, headers=self.a).status_code, 415)
        self.assertEqual(self.client.put(f"/api/uploads/{uid}?offset=0", content=b"x", headers=self.b).status_code, 404)

    def test_templates_and_voices(self) -> None:
        t = self.client.get("/api/templates").json()
        self.assertEqual([x["id"] for x in t["templates"]], ["cinematic_hud", "viral_subtitles", "meme_soundboard"])
        self.assertEqual([b["label"] for b in t["story_beats"]], ["Hook", "Chaos", "Payoff", "Comment Bait"])
        self.assertEqual(t["plan"]["price_cents"], 3000)
        self.assertIn("rvc_voices", self.client.get("/api/voices", headers=self.a).json())

    def test_frontend_served_with_security_headers(self) -> None:
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("GTA6 Clipper Studio", r.text)
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(self.client.get("/app.js").status_code, 200)

    def test_per_account_queue_cap(self) -> None:
        uid = self.upload(self.a)
        blocker = asyncio.Event()

        async def stuck(settings, job, source, options, out_dir):  # type: ignore[no-untyped-def]
            await blocker.wait()
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            s = Settings(data_root=Path(tmp), dev_billing=True, auth_rate=(1000, 60.0))
            with TestClient(create_app(s, pipeline=stuck)) as c:
                tok = c.post("/api/auth/register", json={"email": "c@example.test", "password": "longpassword1"}).json()["token"]
                h = {"Authorization": f"Bearer {tok}"}
                c.post("/api/billing/dev-activate", json={}, headers=h)
                init = c.post("/api/uploads", json={"filename": "s.mp4", "total_bytes": 3}, headers=h).json()
                c.put(f"/api/uploads/{init['upload_id']}?offset=0", content=b"abc", headers=h)
                c.post(f"/api/uploads/{init['upload_id']}/complete", json={}, headers=h)
                codes = [c.post("/api/jobs", json={"upload_id": init["upload_id"]}, headers=h).status_code for _ in range(4)]
                second = c.get("/api/jobs", headers=h).json()["jobs"]
                self.assertTrue(any(j["queue_position"] == 1 and j["eta_sec"] is not None for j in second))
                jid = second[0]["job_id"]
                self.assertTrue(c.post(f"/api/jobs/{jid}/cancel", headers=h).json()["cancelled"])
        self.assertEqual(codes, [200, 200, 200, 429])
        self.assertTrue(uid)


@unittest.skipUnless(helpers.FFMPEG and helpers.FFPROBE, "ffmpeg/ffprobe not on PATH")
class DefaultPipelineEndToEndTests(unittest.TestCase):
    """Real upload -> probe -> chunked activity analysis -> ClipFarm render, through the HTTP API."""

    def test_crash_clip_rendered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            data = helpers.make_crash_video(d / "src.mp4", duration=24.0, crash_at=15.0).read_bytes()
            s = Settings(data_root=d / "data", dev_billing=True, ffmpeg_bin=helpers.FFMPEG, ffprobe_bin=helpers.FFPROBE,
                         chunk_sec=10.0, overlap_sec=3.0, auth_rate=(1000, 60.0))
            with TestClient(create_app(s)) as c:
                tok = c.post("/api/auth/register", json={"email": "e2e@example.test", "password": "longpassword1"}).json()["token"]
                h = {"Authorization": f"Bearer {tok}"}
                c.post("/api/billing/dev-activate", json={}, headers=h)
                init = c.post("/api/uploads", json={"filename": "gta.mp4", "total_bytes": len(data)}, headers=h).json()
                c.put(f"/api/uploads/{init['upload_id']}?offset=0", content=data, headers=h)
                c.post(f"/api/uploads/{init['upload_id']}/complete", json={}, headers=h)
                jid = c.post("/api/jobs", json={"upload_id": init["upload_id"], "templates": [], "max_clips": 2}, headers=h).json()["job_id"]
                for _ in range(600):
                    j = c.get(f"/api/jobs/{jid}", headers=h).json()
                    if j["status"] in ("done", "failed"):
                        break
                    time.sleep(0.1)
                self.assertEqual(j["status"], "done", j)
                ex = c.get("/api/exports", headers=h).json()["exports"]
                self.assertTrue(ex, "no export rendered")
                self.assertIn("CAR_CRASH", ex[0]["tags"])
                clip = c.get(ex[0]["url"], headers=h)
                self.assertEqual(clip.status_code, 200)
                self.assertGreater(len(clip.content), 10_000)


if __name__ == "__main__":
    unittest.main()
