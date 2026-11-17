/* GTA6 Clipper Studio — vanilla client. No build step, no third-party JS. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const TOKEN_KEY = "gtc.token";
  const PART_RETRIES = 4;
  const state = {
    token: storeGet(TOKEN_KEY),
    me: null,
    upload: null, // { upload_id, filename }
    jobs: [],
    exports: [],
    filter: "ALL",
    blobCache: new Map(),
    pollTimer: null,
    authMode: "login",
  };

  function storeGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
  function storeSet(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* private mode */ } }

  function toast(msg, isErr = false) {
    const t = $("toast");
    t.textContent = msg;
    t.classList.toggle("err", isErr);
    t.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.remove("show"), 3200);
  }

  async function api(path, opts = {}) {
    const headers = Object.assign({}, opts.headers || {});
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
    const res = await fetch(path, { method: opts.method || "GET", headers, body: opts.body });
    if (opts.raw) return res;
    let data = null;
    try { data = await res.json(); } catch { /* empty */ }
    if (!res.ok) {
      const d = data && data.detail;
      const msg = typeof d === "string" ? d : (d && (d.error || d.reason)) || `HTTP ${res.status}`;
      const err = new Error(msg); err.status = res.status; err.data = data; throw err;
    }
    return data;
  }

  const fmtBytes = (n) => {
    if (!Number.isFinite(n)) return "–";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
  };
  const fmtEta = (s) => {
    if (s == null || !Number.isFinite(s)) return "–";
    s = Math.max(0, Math.round(s));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60), h = Math.floor(m / 60);
    return h ? `${h}h ${m % 60}m` : `${m}m ${s % 60}s`;
  };
  const TAG_LABEL = { CAR_CRASH: "Crash", POLICE_CHASE: "Chase", CLUTCH_KILL: "Clutch", GUNFIGHT: "Gunfight", REACTION: "Funny" };
  const tagEl = (t) => { const s = document.createElement("span"); s.className = "tag"; s.dataset.tag = t; s.textContent = TAG_LABEL[t] || t; return s; };

  /* ---------------- auth + membership ---------------- */
  async function refreshMe() {
    if (!state.token) { state.me = null; renderMember(); return; }
    try { state.me = await api("/api/me"); }
    catch (e) { if (e.status === 401) { state.token = null; storeSet(TOKEN_KEY, null); } state.me = null; }
    renderMember();
  }

  function isMember() { return !!(state.me && state.me.membership && state.me.membership.active); }

  function renderMember() {
    const pill = $("memberPill"), btn = $("accountBtn");
    if (!state.me) {
      pill.className = "pill pill-muted"; pill.textContent = "Guest"; btn.textContent = "Sign in";
    } else {
      const m = state.me.membership;
      pill.className = "pill " + (m.active ? (m.in_grace ? "pill-warn" : "pill-live") : "pill-muted");
      pill.textContent = m.active ? (m.in_grace ? "Creator · renew" : "Creator") : "Free";
      btn.textContent = "Sign out";
    }
    $("paywall").hidden = !state.me || isMember();
    $("dropzone").classList.toggle("locked", !isMember());
    updateGenerate();
  }

  function openAuth() {
    $("authError").hidden = true;
    $("authDialog").showModal();
    $("authEmail").focus();
  }

  function setAuthMode(mode) {
    state.authMode = mode;
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.mode === mode));
    $("authSubmit").textContent = mode === "login" ? "Sign in" : "Create account";
    $("authPassword").autocomplete = mode === "login" ? "current-password" : "new-password";
  }

  async function submitAuth(ev) {
    ev.preventDefault();
    const body = { email: $("authEmail").value.trim(), password: $("authPassword").value };
    try {
      const res = await api(`/api/auth/${state.authMode === "login" ? "login" : "register"}`, { method: "POST", json: body });
      state.token = res.token; storeSet(TOKEN_KEY, res.token);
      $("authPassword").value = "";
      $("authDialog").close();
      await refreshMe(); await refreshJobs(); await refreshExports(); await loadVoices();
      toast(state.authMode === "login" ? "Welcome back." : "Account created.");
    } catch (e) {
      $("authError").textContent = e.message; $("authError").hidden = false;
    }
  }

  /* ---------------- chunked resumable upload ---------------- */
  async function putPart(id, offset, blob) {
    for (let attempt = 0; ; attempt++) {
      try {
        return await api(`/api/uploads/${id}?offset=${offset}`, { method: "PUT", body: blob, headers: { "Content-Type": "application/octet-stream" } });
      } catch (e) {
        if (e.status === 409) { // offset drift after a lost response: resync from the server
          const st = await api(`/api/uploads/${id}`);
          return { received_bytes: st.received_bytes, resynced: true };
        }
        if (attempt >= PART_RETRIES || (e.status && e.status < 500 && e.status !== 429)) throw e;
        await new Promise((r) => setTimeout(r, 600 * 2 ** attempt));
      }
    }
  }

  async function uploadFile(file) {
    if (!state.me) { openAuth(); return; }
    if (!isMember()) { toast("A Creator membership is required to upload.", true); return; }
    $("uploadBox").hidden = false;
    $("uploadName").textContent = file.name;
    state.upload = null; updateGenerate();
    try {
      const init = await api("/api/uploads", { method: "POST", json: { filename: file.name, total_bytes: file.size } });
      const part = init.part_bytes;
      let offset = 0;
      const t0 = performance.now();
      while (offset < file.size) {
        const res = await putPart(init.upload_id, offset, file.slice(offset, Math.min(file.size, offset + part)));
        offset = res.received_bytes;
        const pct = offset / file.size;
        const secs = (performance.now() - t0) / 1000;
        const rate = offset / Math.max(secs, 0.001);
        $("uploadBar").style.width = `${(pct * 100).toFixed(1)}%`;
        $("uploadStat").textContent = `${fmtBytes(offset)} / ${fmtBytes(file.size)} · ${fmtBytes(rate)}/s · ETA ${fmtEta((file.size - offset) / rate)}`;
      }
      await api(`/api/uploads/${init.upload_id}/complete`, { method: "POST", json: {} });
      state.upload = { upload_id: init.upload_id, filename: init.filename };
      $("uploadStat").textContent = `${fmtBytes(file.size)} · ready`;
      toast("Upload complete. Pick templates and generate.");
    } catch (e) {
      $("uploadStat").textContent = `failed: ${e.message}`;
      toast(`Upload failed: ${e.message}`, true);
    }
    updateGenerate();
  }

  /* ---------------- jobs ---------------- */
  function selectedTemplates() {
    return [...document.querySelectorAll("#templateToggles input:checked")].map((i) => i.value);
  }

  function updateGenerate() {
    const ok = isMember() && !!state.upload;
    $("generate").disabled = !ok;
    $("generateHint").textContent = !state.me ? "Sign in to start." : !isMember() ? "Creator membership required." :
      state.upload ? `Source: ${state.upload.filename}` : "Upload a session first.";
  }

  async function generate() {
    if (!state.upload) return;
    const provider = $("provider").value;
    const voice = $("voice").value || null;
    const body = {
      upload_id: state.upload.upload_id,
      templates: selectedTemplates(),
      story_mode: $("storyMode").checked,
      story_target_sec: Number($("storyLen").value),
      custom_script: $("script").value.trim() || null,
      tts_provider: provider,
      voice,
      rvc_pitch: provider === "rvc_voice" ? Number($("pitch").value) : null,
      language: $("language").value,
    };
    try {
      $("generate").disabled = true;
      await api("/api/jobs", { method: "POST", json: body });
      toast("Queued. Watching progress…");
      await refreshJobs();
    } catch (e) { toast(e.message, true); }
    updateGenerate();
  }

  async function refreshJobs() {
    if (!state.me) { state.jobs = []; renderJobs(); return; }
    try { state.jobs = (await api("/api/jobs")).jobs; } catch { /* keep last */ }
    renderJobs();
    const active = state.jobs.some((j) => j.status === "queued" || j.status === "running");
    clearTimeout(state.pollTimer);
    if (active) state.pollTimer = setTimeout(async () => { await refreshJobs(); }, 2000);
    else if (state._wasActive) await refreshExports();
    state._wasActive = active;
  }

  function renderJobs() {
    const ul = $("queueList");
    ul.replaceChildren();
    if (!state.jobs.length) { const li = document.createElement("li"); li.className = "muted small"; li.textContent = "No jobs yet."; ul.append(li); }
    for (const j of state.jobs.slice(0, 8)) {
      const li = document.createElement("li"); li.className = "job";
      const head = document.createElement("div"); head.className = "job-head";
      const left = document.createElement("span");
      const st = document.createElement("span"); st.className = `job-status ${j.status}`; st.textContent = j.status;
      left.append(st, document.createTextNode(`  ${j.stage || ""}`));
      const right = document.createElement("span"); right.className = "muted";
      if (j.status === "queued") right.textContent = `#${j.queue_position ?? "?"} in line · ETA ${fmtEta(j.eta_sec)}`;
      else if (j.status === "running") right.textContent = `${Math.round(j.progress * 100)}% · ETA ${fmtEta(j.eta_sec)}`;
      else if (j.error) right.textContent = j.error.slice(0, 80);
      head.append(left, right);
      const bar = document.createElement("div"); bar.className = "bar";
      const fill = document.createElement("div"); fill.className = "bar-fill"; fill.style.width = `${(j.progress || 0) * 100}%`;
      bar.append(fill);
      li.append(head, bar);
      if (j.status === "queued" || j.status === "running") {
        const c = document.createElement("button"); c.className = "btn btn-ghost btn-sm"; c.type = "button"; c.textContent = "Cancel";
        c.style.marginTop = "8px";
        c.addEventListener("click", async () => { try { await api(`/api/jobs/${j.job_id}/cancel`, { method: "POST" }); refreshJobs(); } catch (e) { toast(e.message, true); } });
        li.append(c);
      }
      ul.append(li);
    }
    const running = state.jobs.filter((j) => j.status === "running").length;
    const queued = state.jobs.filter((j) => j.status === "queued").length;
    $("queueSummary").textContent = state.jobs.length ? `${running} running · ${queued} queued` : "";
  }

  /* ---------------- exports library + player ---------------- */
  async function refreshExports() {
    if (!state.me) { state.exports = []; renderLibrary(); return; }
    try { state.exports = (await api("/api/exports")).exports; } catch { /* keep last */ }
    renderFilters(); renderLibrary();
  }

  function renderFilters() {
    const tags = new Set(); state.exports.forEach((x) => x.tags.forEach((t) => tags.add(t)));
    const box = $("tagFilters"); box.replaceChildren();
    for (const t of ["ALL", ...tags]) {
      const b = document.createElement("button"); b.type = "button"; b.textContent = t === "ALL" ? "All" : (TAG_LABEL[t] || t);
      b.classList.toggle("on", state.filter === t);
      b.addEventListener("click", () => { state.filter = t; renderFilters(); renderLibrary(); });
      box.append(b);
    }
  }

  function renderLibrary() {
    const grid = $("library"); grid.replaceChildren();
    const items = state.exports.filter((x) => state.filter === "ALL" || x.tags.includes(state.filter));
    if (!items.length) { const p = document.createElement("p"); p.className = "muted small"; p.textContent = "Rendered 9:16 clips appear here."; grid.append(p); return; }
    items.forEach((x, i) => {
      const card = document.createElement("button"); card.type = "button"; card.className = "clip"; card.dataset.url = x.url;
      const thumb = document.createElement("div"); thumb.className = "clip-thumb"; thumb.textContent = String(i + 1).padStart(2, "0");
      const score = document.createElement("span"); score.className = "clip-score"; score.textContent = x.score != null ? Math.round(x.score) : "";
      const info = document.createElement("div"); info.className = "clip-info";
      const tags = document.createElement("div"); tags.className = "tag-row"; x.tags.forEach((t) => tags.append(tagEl(t)));
      const dur = document.createElement("div"); dur.className = "small muted"; dur.textContent = x.duration_sec ? `${x.duration_sec.toFixed(1)}s` : "";
      info.append(tags, dur);
      card.append(thumb, score, info);
      card.addEventListener("click", () => play(x, card));
      grid.append(card);
    });
  }

  async function blobUrl(url) {
    if (state.blobCache.has(url)) return state.blobCache.get(url);
    const res = await api(url, { raw: true });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const obj = URL.createObjectURL(await res.blob());
    state.blobCache.set(url, obj);
    return obj;
  }

  async function play(x, card) {
    document.querySelectorAll(".clip.active").forEach((c) => c.classList.remove("active"));
    card.classList.add("active");
    try {
      const src = await blobUrl(x.url);
      const v = $("player"); v.src = src; $("playerEmpty").hidden = true;
      v.play().catch(() => { /* autoplay blocked: controls remain */ });
      $("playerTitle").textContent = x.filename;
      const tr = $("playerTags"); tr.replaceChildren(...x.tags.map(tagEl));
      const dl = document.createElement("a"); dl.href = src; dl.download = x.filename; dl.className = "btn btn-ghost btn-sm"; dl.textContent = "Download";
      tr.append(dl);
      const byBeat = Object.fromEntries((x.beats || []).map((b) => [b.beat, b.text]));
      document.querySelectorAll("#beatRail li").forEach((li) => {
        const text = byBeat[li.dataset.beat];
        li.classList.toggle("on", !!text);
        if (text) li.title = text; else li.removeAttribute("title");
      });
    } catch (e) { toast(`Cannot load clip: ${e.message}`, true); }
  }

  /* ---------------- voices ---------------- */
  async function loadVoices() {
    if (!state.me) return;
    try {
      const { rvc_voices } = await api("/api/voices");
      const sel = $("voice"); sel.replaceChildren(new Option("Default", ""));
      rvc_voices.forEach((v) => sel.append(new Option(v.label || v.id, v.id)));
    } catch { /* optional */ }
  }

  /* ---------------- wiring ---------------- */
  function wire() {
    const dz = $("dropzone"), fi = $("fileInput");
    dz.addEventListener("click", () => { if (!state.me) openAuth(); else if (isMember()) fi.click(); else toast("Creator membership required.", true); });
    dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); dz.click(); } });
    fi.addEventListener("change", () => { if (fi.files[0]) uploadFile(fi.files[0]); fi.value = ""; });
    ["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
    dz.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f) uploadFile(f); });

    $("accountBtn").addEventListener("click", () => {
      if (state.me) { state.token = null; storeSet(TOKEN_KEY, null); state.me = null; state.upload = null; renderMember(); refreshJobs(); refreshExports(); }
      else openAuth();
    });
    document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => setAuthMode(t.dataset.mode)));
    $("authForm").addEventListener("submit", submitAuth);
    $("authCancel").addEventListener("click", () => $("authDialog").close());

    $("storyMode").addEventListener("change", (e) => { $("storyOpts").hidden = !e.target.checked; });
    $("storyLen").addEventListener("input", (e) => { $("storyLenOut").textContent = e.target.value; });
    $("pitch").addEventListener("input", (e) => { const v = Number(e.target.value); $("pitchOut").textContent = v > 0 ? `+${v}` : String(v); });
    $("script").addEventListener("input", (e) => { $("scriptCount").textContent = `${e.target.value.length} / 1800`; });
    $("generate").addEventListener("click", generate);

    $("devActivate").addEventListener("click", async () => {
      try { await api("/api/billing/dev-activate", { method: "POST", json: { days: 30 } }); await refreshMe(); toast("Dev membership active."); }
      catch (e) { toast(e.message, true); }
    });
  }

  async function boot() {
    wire();
    try { $("devActivate").hidden = !(await api("/api/health")).dev_billing; } catch { /* offline */ }
    await refreshMe();
    if (state.me) await Promise.all([refreshJobs(), refreshExports(), loadVoices()]);
  }

  boot();
})();
