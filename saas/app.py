"""
GTA6-Clipper SaaS API (FastAPI).

    uvicorn saas.app:create_app --factory --app-dir gta6-clipper --port 8787

Auth: `Authorization: Bearer <session token>` (from /api/auth/login) or `X-API-Key: gtc_...`.
Uploads and jobs need an active Creator membership ($30/mo); otherwise the API answers 402.
Every handler validates types, finiteness (no NaN / inf) and ranges server-side, rate-limits
auth and upload calls, and scopes every row by the caller's user id.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

APP_ROOT = Path(__file__).resolve().parents[1]  # gta6-clipper/
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from core_engine import engine as engine_mod  # noqa: E402
from core_engine.ingestion import DEFAULT_MAX_PART_BYTES, DEFAULT_MAX_UPLOAD_BYTES, UploadError, UploadStore, analyze_long_form, probe_duration  # noqa: E402
from core_engine.jobs import Job, JobQueue  # noqa: E402
from saas import billing, security  # noqa: E402
from saas.db import Database  # noqa: E402

TTS_PROVIDERS = ("kokoro_local", "offline_local", "neural_edge", "elevenlabs", "custom_voice_clone", "rvc_voice")
MAX_CUSTOM_SCRIPT_CHARS = 1800
VOICE_RE = r"^[A-Za-z0-9:_ .\-]{1,80}$"
EXPORT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,160}\.(mp4|ass|srt|json|jpg)$")
JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")

PipelineFn = Callable[["Settings", Job, Path, dict[str, Any], Path], Awaitable[dict[str, Any]]]


@dataclass
class Settings:
    database_url: str = "sqlite:///:memory:"
    data_root: Path = field(default_factory=lambda: APP_ROOT / ".data")
    session_secret: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    webhook_secret: str = ""
    dev_billing: bool = False  # enables POST /api/billing/dev-activate (local demos only)
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    max_part_bytes: int = DEFAULT_MAX_PART_BYTES
    gpu_concurrency: int = 1
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    chunk_sec: float = 300.0
    overlap_sec: float = 15.0
    auth_rate: tuple[int, float] = (10, 60.0)  # 10 attempts / minute / ip
    api_rate: tuple[int, float] = (600, 60.0)  # 600 calls / minute / user (upload parts included)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        s = cls(
            database_url=env.get("GTC_DATABASE_URL", f"sqlite:///{(APP_ROOT / '.data' / 'gtc.sqlite3').as_posix()}"),
            data_root=Path(env.get("GTC_DATA_ROOT", str(APP_ROOT / ".data"))),
            webhook_secret=env.get("GTC_WEBHOOK_SECRET", ""),
            dev_billing=env.get("GTC_DEV_BILLING", "") == "1",
            gpu_concurrency=int(env.get("GTC_GPU_CONCURRENCY", "1")),
            ffmpeg_bin=env.get("GTC_FFMPEG", "ffmpeg"),
            ffprobe_bin=env.get("GTC_FFPROBE", "ffprobe"),
        )
        secret = env.get("GTC_SESSION_SECRET")
        if secret:
            s.session_secret = secret
        return s

    @property
    def uploads_root(self) -> Path:
        return self.data_root / "uploads"

    @property
    def exports_root(self) -> Path:
        return self.data_root / "exports"


class RateLimiter:
    """Sliding-window limiter, in process. Use Redis for a multi-instance deployment."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window: float) -> bool:
        t = time.monotonic()
        q = self._hits[key]
        while q and q[0] <= t - window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(t)
        return True


# ------------------------------------------------------------------------------
# Request models
# ------------------------------------------------------------------------------

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Credentials(Strict):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=security.MIN_PASSWORD_LEN, max_length=security.MAX_PASSWORD_LEN)


class LoginBody(Strict):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=security.MAX_PASSWORD_LEN)


class KeyBody(Strict):
    label: str = Field(default="default", min_length=1, max_length=60)


class UploadInit(Strict):
    filename: str = Field(min_length=1, max_length=255)
    total_bytes: int = Field(gt=0)


class UploadComplete(Strict):
    sha256: str | None = Field(default=None, min_length=64, max_length=64)


class JobBody(Strict):
    upload_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    templates: list[str] = Field(default_factory=lambda: ["viral_subtitles"], max_length=len(engine_mod.TEMPLATES))
    story_mode: bool = False
    story_target_sec: float = Field(default=45.0, ge=30.0, le=60.0, allow_inf_nan=False)
    custom_script: str | None = Field(default=None, max_length=MAX_CUSTOM_SCRIPT_CHARS)
    tts_provider: str = "offline_local"
    voice: str | None = Field(default=None, pattern=VOICE_RE)
    rvc_pitch: int | None = Field(default=None, ge=-12, le=12)
    language: str = Field(default="EN", pattern=r"^(EN|ES)$")
    max_clips: int = Field(default=6, ge=1, le=20)


class DevActivate(Strict):
    days: int = Field(default=30, ge=1, le=366)


# ------------------------------------------------------------------------------
# Default pipeline: probe -> chunked analysis -> sequence render
# ------------------------------------------------------------------------------

async def default_pipeline(settings: Settings, job: Job, source: Path, options: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    queue: JobQueue = options.pop("_queue")
    duration = await asyncio.to_thread(probe_duration, settings.ffprobe_bin, source)

    async def on_chunk(frac: float, label: str) -> None:
        await queue.set_progress(job, 0.05 + 0.40 * frac, f"analyzing {label}")

    analysis = await analyze_long_form(
        source, duration, settings.ffmpeg_bin, settings.chunk_sec, settings.overlap_sec,
        max_moments=int(options["max_clips"]), progress=on_chunk, cancel=job.cancel,
    )
    await queue.set_progress(job, 0.45, f"rendering {len(analysis['moments'])} clips")
    eng = engine_mod.VideoEngine(ffmpeg_bin=settings.ffmpeg_bin, ffprobe_bin=settings.ffprobe_bin)
    opts = engine_mod.RenderOptions(
        templates=options["templates"], story_mode=options["story_mode"], story_target_sec=options["story_target_sec"],
        custom_script=options["custom_script"], tts_provider=options["tts_provider"], voice=options["voice"],
        rvc_pitch=options["rvc_pitch"], language=options["language"],
    )

    def render_progress(frac: float) -> None:  # called from the render thread: plain attribute writes only
        job.progress = max(job.progress, 0.45 + 0.55 * frac)
        job.stage = "rendering"

    exports = await asyncio.to_thread(eng.render_sequence, source, analysis["moments"], out_dir, opts, duration, render_progress)
    return {"analysis": analysis, "exports": exports}


# ------------------------------------------------------------------------------
# App factory
# ------------------------------------------------------------------------------

def create_app(settings: Settings | None = None, pipeline: PipelineFn | None = None, engine: Any = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_root.mkdir(parents=True, exist_ok=True)
    db = Database(settings.database_url)
    store = UploadStore(settings.uploads_root, settings.max_upload_bytes, settings.max_part_bytes)
    limiter = RateLimiter()
    run_pipeline = pipeline or default_pipeline
    voice_engine = engine or engine_mod.VideoEngine()

    async def persist(job: Job) -> None:
        db.update_job(job.job_id, job.status, job.progress, job.stage, job.result, job.error,
                      job.status in ("done", "failed", "cancelled"))

    queue = JobQueue(settings.gpu_concurrency, listener=persist)

    @asynccontextmanager
    async def lifespan(_: FastAPI):  # type: ignore[no-untyped-def]
        await queue.start()
        try:
            yield
        finally:
            await queue.stop()

    app = FastAPI(title="GTA6-Clipper Studio API", version="0.1.0", lifespan=lifespan)
    app.state.settings, app.state.db, app.state.queue, app.state.store = settings, db, queue, store

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Content-Security-Policy",
                                "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
                                "style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com")
        return resp

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    # --- auth dependencies ------------------------------------------------
    def current_user(
        request: Request,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        user_id: str | None = None
        if x_api_key:
            row = db.api_key_by_prefix(x_api_key[: security.API_KEY_LOOKUP_CHARS])
            if row and security.api_key_matches(x_api_key, row["key_hash"]):
                user_id = row["user_id"]
                db.touch_api_key(row["id"])
        elif authorization and authorization.lower().startswith("bearer "):
            user_id = security.read_session(authorization[7:].strip(), settings.session_secret)
        user = db.user_by_id(user_id) if user_id else None
        if not user or not user["is_active"]:
            raise HTTPException(401, "authentication required")
        limit, window = settings.api_rate
        if not limiter.allow(f"api:{user['id']}", limit, window):
            raise HTTPException(429, "rate limit exceeded")
        return user

    def member(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        verdict = billing.evaluate_membership(db.membership(user["id"]))
        if not verdict.active:
            raise HTTPException(402, {"error": "membership_required", **verdict.public()})
        return user

    def auth_gate(request: Request) -> None:
        limit, window = settings.auth_rate
        if not limiter.allow(f"auth:{client_ip(request)}", limit, window):
            raise HTTPException(429, "too many attempts, wait a minute")

    def upload_error(exc: UploadError) -> HTTPException:
        return HTTPException(exc.status, str(exc))

    # --- public -----------------------------------------------------------
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "gta6-clipper", "dev_billing": settings.dev_billing,
                "queue_depth": sum(1 for j in queue.jobs.values() if j.status == "queued")}

    @app.get("/api/templates")
    def templates() -> dict[str, Any]:
        return {
            "templates": [{"id": k, "label": v["label"]} for k, v in engine_mod.TEMPLATES.items()],
            "story_beats": [{"beat": k, "label": v} for k, v in engine_mod.STORY_BEAT_LABELS.items()],
            "tts_providers": list(TTS_PROVIDERS),
            "plan": {"id": billing.PLAN_ID, "price_cents": billing.PLAN_PRICE_CENTS, "currency": billing.PLAN_CURRENCY},
        }

    @app.post("/api/auth/register", dependencies=[Depends(auth_gate)])
    def register(body: Credentials) -> dict[str, Any]:
        email = security.normalize_email(body.email)
        if not email:
            raise HTTPException(422, "invalid email")
        if db.user_by_email(email):
            raise HTTPException(409, "email already registered")
        user = db.create_user(secrets.token_hex(16), email, security.hash_password(body.password))
        return {"user_id": user["id"], "email": email, "token": security.sign_session(user["id"], settings.session_secret)}

    @app.post("/api/auth/login", dependencies=[Depends(auth_gate)])
    def login(body: LoginBody) -> dict[str, Any]:
        email = security.normalize_email(body.email)
        user = db.user_by_email(email) if email else None
        # Same error and comparable work for unknown email and bad password.
        stored = user["password_hash"] if user else security.hash_password(secrets.token_hex(8))
        if not security.verify_password(body.password, stored) or not user or not user["is_active"]:
            raise HTTPException(401, "invalid email or password")
        return {"user_id": user["id"], "email": user["email"], "token": security.sign_session(user["id"], settings.session_secret)}

    @app.get("/api/me")
    def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        verdict = billing.evaluate_membership(db.membership(user["id"]))
        return {"user_id": user["id"], "email": user["email"], "membership": verdict.public()}

    # --- api keys ---------------------------------------------------------
    @app.get("/api/keys")
    def list_keys(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        return {"keys": db.list_api_keys(user["id"])}

    @app.post("/api/keys")
    def create_key(body: KeyBody, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        if sum(1 for k in db.list_api_keys(user["id"]) if k["revoked_at"] is None) >= 10:
            raise HTTPException(409, "at most 10 active API keys")
        key, prefix, digest = security.new_api_key()
        key_id = secrets.token_hex(16)
        db.add_api_key(key_id, user["id"], prefix, digest, body.label)
        return {"id": key_id, "key": key, "prefix": prefix, "label": body.label, "note": "shown once; store it now"}

    @app.delete("/api/keys/{key_id}")
    def revoke_key(key_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        if not db.revoke_api_key(user["id"], key_id):
            raise HTTPException(404, "key not found")
        return {"revoked": key_id}

    # --- billing ----------------------------------------------------------
    @app.api_route("/api/billing/verify-membership", methods=["GET", "POST"])
    def verify_membership(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        return billing.evaluate_membership(db.membership(user["id"])).public()

    @app.post("/api/billing/webhook")
    async def billing_webhook(request: Request, x_billing_signature: str | None = Header(default=None)) -> dict[str, Any]:
        body = await request.body()
        if len(body) > 64 * 1024:
            raise HTTPException(413, "payload too large")
        if not billing.verify_webhook_signature(body, x_billing_signature, settings.webhook_secret):
            raise HTTPException(400, "invalid signature")
        try:
            row = billing.membership_from_event(json.loads(body))
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        if not db.user_by_id(row["user_id"]):
            raise HTTPException(404, "unknown user")
        if not db.claim_webhook_event(row["event_id"]):
            return {"ok": True, "duplicate": True}
        db.upsert_membership(row["user_id"], row["plan"], row["status"], row["price_cents"], row["currency"],
                             row["period_end"], "webhook", row["provider_ref"])
        return {"ok": True, "duplicate": False, "status": row["status"]}

    @app.post("/api/billing/dev-activate")
    def dev_activate(body: DevActivate, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        if not settings.dev_billing:
            raise HTTPException(404, "not found")
        db.upsert_membership(user["id"], billing.PLAN_ID, "active", billing.PLAN_PRICE_CENTS, billing.PLAN_CURRENCY,
                             int(time.time()) + body.days * 86400, "dev", None)
        return billing.evaluate_membership(db.membership(user["id"])).public()

    # --- voices -----------------------------------------------------------
    @app.get("/api/voices")
    def voices(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        return {"rvc_voices": [{"id": v.get("id"), "label": v.get("label"), "lang": v.get("lang")} for v in voice_engine.voices()]}

    # --- uploads ----------------------------------------------------------
    @app.post("/api/uploads")
    def create_upload(body: UploadInit, user: dict[str, Any] = Depends(member)) -> dict[str, Any]:
        try:
            rec = store.create(user["id"], body.filename, body.total_bytes)
        except UploadError as exc:
            raise upload_error(exc) from exc
        db.add_upload(rec.upload_id, user["id"], rec.filename, rec.total_bytes)
        return {"upload_id": rec.upload_id, "filename": rec.filename, "part_bytes": settings.max_part_bytes, "received_bytes": 0}

    @app.get("/api/uploads/{upload_id}")
    def upload_status(upload_id: str, user: dict[str, Any] = Depends(member)) -> dict[str, Any]:
        try:
            rec = store.get(upload_id, user["id"])
        except UploadError as exc:
            raise upload_error(exc) from exc
        return {"upload_id": rec.upload_id, "filename": rec.filename, "total_bytes": rec.total_bytes,
                "received_bytes": rec.received_bytes, "status": rec.status}

    @app.put("/api/uploads/{upload_id}")
    async def upload_part(upload_id: str, offset: int, request: Request, user: dict[str, Any] = Depends(member)) -> dict[str, Any]:
        if offset < 0:
            raise HTTPException(422, "offset must be >= 0")
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > settings.max_part_bytes):
            raise HTTPException(413, f"part exceeds {settings.max_part_bytes} bytes")
        buf = bytearray()
        async for piece in request.stream():
            buf.extend(piece)
            if len(buf) > settings.max_part_bytes:
                raise HTTPException(413, f"part exceeds {settings.max_part_bytes} bytes")
        try:
            rec = await asyncio.to_thread(store.append, upload_id, user["id"], offset, bytes(buf))
        except UploadError as exc:
            raise upload_error(exc) from exc
        return {"upload_id": rec.upload_id, "received_bytes": rec.received_bytes, "total_bytes": rec.total_bytes}

    @app.post("/api/uploads/{upload_id}/complete")
    async def upload_complete(upload_id: str, body: UploadComplete, user: dict[str, Any] = Depends(member)) -> dict[str, Any]:
        try:
            rec = await asyncio.to_thread(store.complete, upload_id, user["id"], body.sha256)
        except UploadError as exc:
            raise upload_error(exc) from exc
        db.set_upload_status(upload_id, "complete")
        return {"upload_id": rec.upload_id, "status": rec.status, "sha256": rec.sha256}

    # --- jobs -------------------------------------------------------------
    @app.post("/api/jobs")
    def create_job(body: JobBody, user: dict[str, Any] = Depends(member)) -> dict[str, Any]:
        unknown = [t for t in body.templates if t not in engine_mod.TEMPLATES]
        if unknown:
            raise HTTPException(422, f"unknown templates: {unknown}")
        if body.tts_provider not in TTS_PROVIDERS:
            raise HTTPException(422, "unknown tts_provider")
        try:
            rec = store.get(body.upload_id, user["id"])
        except UploadError as exc:
            raise upload_error(exc) from exc
        if rec.status != "complete" or not rec.path:
            raise HTTPException(409, "upload not complete")
        active = sum(1 for j in queue.jobs.values() if j.owner_id == user["id"] and j.status in ("queued", "running"))
        if active >= 3:
            raise HTTPException(429, "at most 3 queued jobs per account")
        options = body.model_dump()
        options["templates"] = list(dict.fromkeys(body.templates))
        job_id = secrets.token_hex(16)
        out_dir = settings.exports_root / user["id"] / job_id
        source = Path(rec.path)
        size_min = max(1.0, rec.total_bytes / (60 * 1024**2))  # ~60 MB per source minute until probed

        async def run(job: Job) -> dict[str, Any]:
            return await run_pipeline(settings, job, source, {**options, "_queue": queue}, out_dir)

        db.add_job(job_id, user["id"], body.upload_id, options)
        queue.submit(user["id"], size_min, run, job_id=job_id)
        return queue.snapshot(job_id) or {}

    def owned_job(job_id: str, user: dict[str, Any]) -> dict[str, Any]:
        if not JOB_ID_RE.match(job_id):
            raise HTTPException(404, "job not found")
        row = db.job(job_id, user["id"])
        if not row:
            raise HTTPException(404, "job not found")
        return row

    def job_view(row: dict[str, Any]) -> dict[str, Any]:
        live = queue.snapshot(row["id"])
        out = live or {"job_id": row["id"], "status": row["status"], "progress": row["progress"], "stage": row["stage"],
                       "error": row["error"], "queue_position": None, "eta_sec": None}
        out["options"] = json.loads(row["options_json"])
        out["created_at"] = row["created_at"]
        return out

    @app.get("/api/jobs")
    def list_jobs(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        return {"jobs": [job_view(r) for r in db.jobs_for(user["id"])]}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        return job_view(owned_job(job_id, user))

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        owned_job(job_id, user)
        job = queue.jobs.get(job_id)
        ok = queue.cancel(job_id)
        if ok and job is not None and job.status == "cancelled":
            await persist(job)
        return {"cancelled": ok}

    # --- exports ----------------------------------------------------------
    @app.get("/api/exports")
    def list_exports(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for row in db.jobs_for(user["id"]):
            result = json.loads(row["result_json"]) if row["result_json"] else {}
            for ex in result.get("exports", []):
                if not ex.get("ok"):
                    continue
                name = ex.get("output_filename")
                items.append({
                    "job_id": row["id"], "filename": name, "url": f"/api/exports/{row['id']}/{name}",
                    "tags": ex.get("moment", {}).get("tags", []), "score": ex.get("moment", {}).get("score"),
                    "duration_sec": (ex.get("render") or {}).get("verified_duration_sec"),
                    "beats": (ex.get("narration") or {}).get("beats", []), "created_at": row["finished_at"],
                })
        return {"exports": items}

    @app.get("/api/exports/{job_id}/{filename}")
    def get_export(job_id: str, filename: str, user: dict[str, Any] = Depends(current_user)) -> FileResponse:
        owned_job(job_id, user)
        if not EXPORT_NAME_RE.match(filename):
            raise HTTPException(404, "not found")
        base = (settings.exports_root / user["id"] / job_id).resolve()
        target = (base / filename).resolve()
        if target.parent != base or not target.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(target)

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default handler echoes the offending input, and a NaN / Infinity input then
        # crashes JSON rendering (500). Report location and message only.
        errors = [{"loc": [str(p) for p in e.get("loc", ())], "msg": str(e.get("msg", ""))[:200], "type": e.get("type")}
                  for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    frontend = APP_ROOT / "frontend"
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=str(frontend), html=True), name="frontend")
    return app
