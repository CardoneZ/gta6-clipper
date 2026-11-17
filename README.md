# GTA6-Clipper Studio

GTA6-Clipper Studio is a creator SaaS that takes long open-world gameplay sessions (multi-hour GTA V / VI recordings), finds the crashes, police chases, clutch kills, gunfights and funny moments in them, and renders 9:16 TikTok / Shorts / Reels clips. Clips can carry kinetic subtitles, RVC character-voice narration, and a long-form story arc (Hook → Chaos → Payoff → Comment Bait).

The rendering, TTS/RVC and vision layers are **not forked**. `core_engine.VideoEngine` is an adapter over the Layer 1 [`zorvex_clip_farm`](../zorvex_clip_farm/) modules, so ClipFarm fixes reach this product unchanged and ClipFarm's own suite (`scripts/validate.ps1`) keeps guarding them. Nothing under `prototypes/` is touched or imported.

```text
gta6-clipper/
├── core_engine/          # standalone backend engine (no web framework imports)
│   ├── chunking.py       # sliding-window chunk planner, overlap ownership, top-moment selection
│   ├── activity.py       # bounded-memory audio/motion streaming + moment detectors
│   ├── ingestion.py      # resumable chunked upload store + async long-form analysis driver
│   ├── jobs.py           # asyncio GPU queue with queue position + learned ETA
│   └── engine.py         # VideoEngine: ClipFarm composer / tts / RVC / vision / captions adapter
├── saas/
│   ├── db.py             # tenancy schema (SQLite now, PostgreSQL-ready)
│   ├── security.py       # scrypt passwords, HMAC session tokens, hashed API keys
│   ├── billing.py        # $30/mo Creator membership verdicts + signed billing webhooks
│   └── app.py            # FastAPI app (auth, keys, billing, uploads, jobs, exports, static UI)
├── frontend/             # no-build vanilla UI: index.html, app.css, app.js
├── tests/                # 80 unit + integration tests (real FFmpeg where available)
└── tools/check.ps1       # local validation gate
```

## Quick start (local)

Run every command from the monorepo root. They use the existing `.venv` read-only and need FFmpeg on `PATH`.

```powershell
$env:GTC_DEV_BILLING = "1"   # local only: shows "Activate (dev)" so you can skip a real payment
.\.venv\Scripts\python.exe -B -m uvicorn saas.app:create_app --factory --app-dir gta6-clipper --port 8787
```

Open `http://127.0.0.1:8787`. Create an account, click **Activate (dev)**, drop a recording and press **Generate clips**. `.claude/launch.json` holds the same command as `gta6-clipper-dev`.

## Pipeline

```text
browser ──PUT parts (16 MB, resumable)──▶ UploadStore ──complete + SHA-256──▶ JobQueue (GPU slots)
                                                                                 │
           ffprobe duration ◀────────────────────────────────────────────────────┘
                 │
     plan_chunks(5 min core, ±15 s overlap) ──▶ per chunk, in a worker thread:
                 │                               ffmpeg → 8 kHz mono PCM → RMS dB per 0.1 s hop
                 │                               ffmpeg → 64×36 gray @ 4 fps → frame-diff motion
                 │                               detect_moments → CAR_CRASH / POLICE_CHASE / CLUTCH_KILL / GUNFIGHT / REACTION
                 ▼
     owned_moments (peak inside core span: zero duplicates) → select_top_moments (score, 20 s gap)
                 ▼
     VideoEngine.render_sequence, per moment:
       story_arc_script (custom script wins) → synthesize_tts (Kokoro / SAPI / Edge / ElevenLabs / warm RVC)
       → long_form_window (30–60 s arc) / fit_window_to_narration → kinetic ASS captions
       → render_vertical_clip (h264_nvenc, libx264 fallback) → optional meme-SFX mix (stream copy)
```

**Memory bound.** RAM and VRAM do not grow with source length. Per job, only one FFmpeg audio pipe and one FFmpeg motion pipe are alive. Each 5 minute chunk becomes about 3,000 floats per signal. A 6 hour session is 72 chunks, analysed one after another.

**Detectors.** The detectors are explainable signal heuristics, not a trained model:

| Tag | Signal rule |
| :-- | :-- |
| `CAR_CRASH` | Loudness jumps 9 dB or more within 0.3 s, and a motion spike (robust z ≥ 2) lands within ±0.5 s |
| `CLUTCH_KILL` | 3 or more sharp onsets within 4 s, then a sudden drop below median loudness within 3 s |
| `GUNFIGHT` | The same onset burst, without the resolving drop |
| `POLICE_CHASE` | Motion z ≥ 1 and loudness z ≥ 0.5 held for 8 s or more (gaps up to 1 s bridged) |
| `REACTION` | Loudness z ≥ 2.5 while the screen is calm (voice chat, laughter); shown as "Funny" in the UI |

Overlapping detections merge: tags combine and the clip gets a combo bonus. The Ollama/Qwen vision director (`ai_vision_director`) can re-rank and caption these candidates afterwards.

**Templates.**

| Template | Effect |
| :-- | :-- |
| `cinematic_hud` | Tighter 1.35× action crop |
| `viral_subtitles` | ClipFarm kinetic word-pop ASS captions, timed to the narration words |
| `meme_soundboard` | Mixes a tag-matched SFX at the moment's peak from `07_TEMPLATES/sfx/` (`vine_boom`, `airhorn`, `siren`, `crash`, `laugh`, …). The video stream is copied, not re-encoded |

## API

| Method | Path | Auth | Purpose |
| :-- | :-- | :-- | :-- |
| GET | `/api/health` | none | Liveness, queue depth, dev-billing flag |
| GET | `/api/templates` | none | Templates, story beats, TTS providers, plan price |
| POST | `/api/auth/register` · `/api/auth/login` | none, rate-limited | Returns a session token (7 days, HMAC-SHA256) |
| GET | `/api/me` | user | Profile and membership verdict |
| GET/POST/DELETE | `/api/keys[/{id}]` | user | API keys (`gtc_…`, shown once, stored as SHA-256, max 10) |
| GET/POST | `/api/billing/verify-membership` | user | `{active, reason, status, current_period_end, in_grace, price_cents}` |
| POST | `/api/billing/webhook` | `X-Billing-Signature` | Signed, idempotent membership events |
| POST | `/api/billing/dev-activate` | user | 404 unless `GTC_DEV_BILLING=1` |
| GET | `/api/voices` | user | RVC character voices in `07_TEMPLATES/voices/custom/` |
| POST | `/api/uploads` | member | Starts an upload `{filename, total_bytes}` and returns `upload_id` and `part_bytes` |
| PUT | `/api/uploads/{id}?offset=N` | member | Raw part. A 409 on offset drift means: resume from `received_bytes` |
| GET | `/api/uploads/{id}` | member | Upload status, for resume |
| POST | `/api/uploads/{id}/complete` | member | Optional `sha256` check |
| POST | `/api/jobs` | member | `{upload_id, templates[], story_mode, story_target_sec, custom_script, tts_provider, voice, rvc_pitch, language, max_clips}` |
| GET | `/api/jobs` · `/api/jobs/{id}` | user | Status, progress, stage, `queue_position`, `eta_sec` |
| POST | `/api/jobs/{id}/cancel` | user | Cancels a queued or running job |
| GET | `/api/exports` · `/api/exports/{job}/{file}` | user | Exports Library and file download (owner only) |

"user" means `Authorization: Bearer <token>` or `X-API-Key`. "member" means a user whose membership verdict is active; anyone else gets **402** `membership_required`.

**Membership rules.**

- Plan `creator_monthly` costs `3000` cents in `usd`.
- `active` and `trialing` grant access until `current_period_end`, plus a 3 day renewal grace.
- `past_due` keeps access through the same grace window.
- `canceled`, an unknown plan, or a price below $30 denies access.

**Webhook contract (provider-neutral).** The provider sends a JSON body `{id, type, data: {user_id, current_period_end, price_cents?, provider_ref?}}`. The `type` is one of `membership.activated|renewed|trialing|past_due|canceled`. The header is `X-Billing-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>`. Requests outside a 5 minute window are refused, and multiple `v1` values are accepted so the secret can rotate. Event ids are deduplicated. To use Stripe, Paddle or Lemon Squeezy, add a thin adapter that maps their events to this shape, or change `verify_webhook_signature` to match their header. Card data never touches this service.

**Hardening in place.**

- Strict Pydantic models reject unknown fields, NaN/Infinity and out-of-range values. Validation errors never echo raw input.
- Every row and file is scoped by the owner's user id. Another tenant's ids return 404.
- Export filenames are allow-listed and their paths resolved, so `..` traversal is blocked.
- Login is rate-limited per IP and the API per user.
- Each account can have at most 3 active jobs.
- Responses carry a CSP, `X-Frame-Options: DENY` and `nosniff`.

## SaaS deployment architecture

```text
                    ┌──────────── CDN / TLS edge (Cloudflare) ────────────┐
 creators ─HTTPS──▶ │  static frontend (cacheable)  ·  /api/* → origin    │
                    └───────────────────────┬──────────────────────────────┘
                                            ▼
              ┌──────── API tier (stateless, N replicas, CPU) ────────┐
              │ uvicorn saas.app:create_app --factory  (GTC_* env)     │
              │ auth · billing webhook · upload parts · job submit     │
              └──────┬──────────────────────┬──────────────────────────┘
                     │                      │
          PostgreSQL (managed)       Object storage (S3 / R2)
          users, keys, memberships,  uploads/<id>/source.*, exports/<user>/<job>/*.mp4
          jobs, webhook_events       (signed URLs for playback/download)
                     │
              ┌──────┴──────── GPU worker tier (1 job per GPU) ────────┐
              │ JobQueue concurrency = GPUs · FFmpeg h264_nvenc · RVC   │
              │ warm worker (.venv-rvc) · Ollama qwen2.5vl (localhost)  │
              └─────────────────────────────────────────────────────────┘
```

**Scale-out steps.** The code today runs as a single node. Each step below keeps the same module boundaries.

1. **Database.** Set `GTC_DATABASE_URL=postgresql://…` and install `psycopg[binary]`. The DDL is portable, and `?` placeholders are rewritten to `%s` automatically.
2. **Queue.** `JobQueue` is in-process. With more than one API replica, run the GPU workers as a separate process that pulls `jobs` rows (`SELECT … FOR UPDATE SKIP LOCKED`) or a Redis/SQS queue. The API then only inserts rows. `queue_position` and `eta_sec` become a count of queued rows ahead × the stored EMA rate.
3. **Storage.** `UploadStore` writes to local disk. On multi-node, mount shared storage, or switch to S3 multipart upload (same `offset`/part protocol, with presigned part URLs) and have workers stream from object storage with FFmpeg `-i https://…`.
4. **Rate limits.** `RateLimiter` is in-process. Move it to Redis (`INCR` + `EXPIRE`) once there is more than one replica.
5. **Secrets.** Set `GTC_SESSION_SECRET` (32+ random bytes; without it, tokens reset on every restart) and `GTC_WEBHOOK_SECRET`. Never set `GTC_DEV_BILLING` in production.
6. **GPU sizing.** Use one NVENC job per consumer GPU (`GTC_GPU_CONCURRENCY=1`). The RVC warm worker and Ollama share VRAM. 12 GB or more is recommended for qwen2.5vl plus RVC.

| Variable | Default | Meaning |
| :-- | :-- | :-- |
| `GTC_DATABASE_URL` | `sqlite:///gta6-clipper/.data/gtc.sqlite3` | `sqlite:///…` or `postgresql://…` |
| `GTC_DATA_ROOT` | `gta6-clipper/.data` | Uploads and exports root (git-ignored) |
| `GTC_SESSION_SECRET` | random per process | HMAC key for session tokens |
| `GTC_WEBHOOK_SECRET` | empty, so every webhook is rejected | Billing webhook signing secret |
| `GTC_DEV_BILLING` | off | `1` enables `/api/billing/dev-activate` |
| `GTC_GPU_CONCURRENCY` | `1` | Parallel render jobs |
| `GTC_FFMPEG` / `GTC_FFPROBE` | `ffmpeg` / `ffprobe` | Binary paths |
| `ZCF_DISABLE_NVENC` | off | ClipFarm switch: force libx264 |
| `ZORVEX_OLLAMA_URL` / `ZORVEX_OLLAMA_MODEL` | `127.0.0.1:11434` | ClipFarm vision director |

## Validation

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File gta6-clipper\tools\check.ps1
```

The gate runs four steps: `py-syntax`, `py-ruff` (`E9,F63,F7,F82`), `py-tests` and `js-syntax` (`node --check`). The tests cover:

- Chunk tiling and overlap ownership
- Detectors on synthetic signals
- FFmpeg streaming readers on generated media
- Membership verdicts and webhook signatures
- The 402 gate, API keys and rate limits
- Resumable uploads and tenant isolation
- Queue position, ETA and cancel
- Story-mode windows and template wiring
- A real ClipFarm render
- A real upload → analysis → render run through the HTTP API

The root gate `scripts/validate.ps1` also syntax-checks and Ruff-checks these files, and the ClipFarm suite stays untouched.

## Naming note

"GTA", "Grand Theft Auto" and "Rockstar Games" are trademarks of Take-Two Interactive. This UI uses an original neon palette and wordmark, with no Rockstar logos, fonts or artwork. Before any paid public launch, have the product name reviewed for trademark use.
