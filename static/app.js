/* ============================================================
   Noriki — client
   Git is the database AND the message bus. There is no server.

   Write ownership is split so the two sides never collide:
     phone writes  → state.json, inbox/*.json
     relay writes  → outbox/*.json, relay-status.json
   Neither ever writes the other's files, so no merge conflicts.
   ============================================================ */

const BUILD = "v1";
const API = "https://api.github.com";
const POLL_MS = 15000;

/* ---------------- local device settings ---------------- */

const LS_TOKEN = "noriki.token";
const LS_REPO = "noriki.repo";
const LS_CACHE = "noriki.cache";
const LS_SEEN = "noriki.seen";

let token = localStorage.getItem(LS_TOKEN) || "";
let repo = localStorage.getItem(LS_REPO) || "";

let state = null;        // the shared state.json
let stateSha = null;     // sha of the file we last read, for safe updates
let seen = JSON.parse(localStorage.getItem(LS_SEEN) || "{}");   // outbox ids already pulled
let currentProject = null;
let pollTimer = null;
let syncing = false;

/* ---------------- tiny helpers ---------------- */

const $ = (id) => document.getElementById(id);
const now = () => new Date().toISOString();

function uid() {
  return now().replace(/[-:.TZ]/g, "").slice(0, 14) + "-" +
         Math.random().toString(36).slice(2, 8);
}

function b64encode(str) {
  const bytes = new TextEncoder().encode(str);
  let bin = "";
  bytes.forEach((b) => { bin += String.fromCharCode(b); });
  return btoa(bin);
}

function b64decode(b64) {
  const bin = atob(b64.replace(/\s/g, ""));
  const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

function ago(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  return Math.floor(s / 86400) + "d";
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function setSync(cls) {
  const d = $("syncDot");
  d.className = "sync-dot" + (cls ? " " + cls : "");
}

/* ---------------- GitHub contents API ---------------- */

async function gh(path, opts) {
  const res = await fetch(API + path, Object.assign({
    headers: {
      Authorization: "Bearer " + token,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28"
    }
  }, opts || {}));
  if (res.status === 404) return null;
  if (!res.ok) {
    const body = await res.text();
    throw new Error(res.status + " " + body.slice(0, 200));
  }
  return res.json();
}

async function readFile(path) {
  const j = await gh(`/repos/${repo}/contents/${encodeURI(path)}`);
  if (!j || !j.content) return null;
  return { text: b64decode(j.content), sha: j.sha };
}

async function writeFile(path, text, sha, message) {
  const body = {
    message: message || ("noriki: " + path),
    content: b64encode(text)
  };
  if (sha) body.sha = sha;
  const j = await gh(`/repos/${repo}/contents/${encodeURI(path)}`, {
    method: "PUT",
    body: JSON.stringify(body)
  });
  return j && j.content ? j.content.sha : null;
}

async function listDir(path) {
  const j = await gh(`/repos/${repo}/contents/${encodeURI(path)}`);
  return Array.isArray(j) ? j : [];
}

/* ---------------- default state ---------------- */

function defaultState() {
  return {
    version: 1,
    updatedAt: now(),
    projects: [
      { id: "film",      name: "Film Festival", kind: "venture · deadline",
        path: "C:\\Users\\Neurasthetic\\Film Festival", due: "2026-09-03" },
      { id: "taleforge", name: "TaleForge",     kind: "venture · flagship",
        path: "C:\\Users\\Neurasthetic\\Documents\\TaleForge" },
      { id: "lootix",    name: "LOOTIX",        kind: "venture · revenue",
        path: "C:\\Users\\Neurasthetic\\Documents\\LOOTIX-live" },
      { id: "ironpath",  name: "IRONPATH",      kind: "practice · health",
        path: "C:\\Users\\Neurasthetic\\bw-mastery" },
      { id: "overseer",  name: "OVERSEER",      kind: "tool",
        path: "C:\\Users\\Neurasthetic\\Documents\\overseer" },
      { id: "noriki",    name: "Noriki",        kind: "tool",
        path: "C:\\Users\\Neurasthetic\\Noriki" }
    ],
    tasks: [],
    checkins: [],
    captures: []
  };
}

/* ---------------- state sync ---------------- */

async function pullState() {
  const f = await readFile("state.json");
  if (!f) {
    state = defaultState();
    stateSha = await writeFile("state.json", JSON.stringify(state, null, 2), null,
                               "noriki: initialise state");
    return;
  }
  try {
    state = JSON.parse(f.text);
  } catch (e) {
    throw new Error("state.json is not valid JSON — fix it in the repo");
  }
  stateSha = f.sha;
  localStorage.setItem(LS_CACHE, JSON.stringify({ state: state, at: now() }));
}

async function pushState() {
  state.updatedAt = now();
  const text = JSON.stringify(state, null, 2);
  try {
    stateSha = await writeFile("state.json", text, stateSha, "noriki: update state");
  } catch (e) {
    // someone else wrote it first — re-read, replay nothing, surface it
    if (String(e.message).startsWith("409")) {
      await pullState();
      throw new Error("State changed elsewhere. Pulled the newer copy — try again.");
    }
    throw e;
  }
  localStorage.setItem(LS_CACHE, JSON.stringify({ state: state, at: now() }));
  $("statPush").textContent = new Date().toLocaleTimeString();
}

/* ---------------- questions from OVERSEER ----------------
   An agent hit a decision it can't make. OVERSEER blocks the session and the
   ask-bridge drops the question here. These jump the manual lane: a blocked
   session is the most expensive thing in the system. */

async function pullAsks() {
  const entries = await listDir("asks");
  const files = entries.filter((e) => e.type === "file" && e.name.endsWith(".json"));
  let added = 0;

  for (const e of files) {
    const qid = e.name.replace(/\.json$/, "");
    const existing = state.tasks.find((t) => t.questionId === qid);

    let doc;
    try {
      const f = await readFile("asks/" + e.name);
      if (!f) continue;
      doc = JSON.parse(f.text);
    } catch (err) { continue; }

    if (doc.state === "answered") {
      if (existing && existing.lane !== "done") {
        existing.lane = "done";
        existing.doneAt = doc.answeredAt || now();
        added++;
      }
      continue;
    }

    if (existing) {
      // an unanswered question can change state (waiting -> expired)
      if (existing.expired !== (doc.state === "expired")) {
        existing.expired = doc.state === "expired";
        existing.why = doc.note || existing.why;
        added++;
      }
      continue;
    }

    state.tasks.unshift({
      id: uid(),
      questionId: qid,
      sessionId: doc.session_id || "",
      project: doc.project || "overseer",
      lane: "manual",
      kind: "ask",
      title: doc.headline || "A session needs a decision",
      why: doc.note || "A session is waiting on you.",
      questions: doc.questions || [],
      expired: doc.state === "expired",
      createdAt: doc.askedAt || now()
    });
    added++;
  }
  return added;
}

async function sendAnswer(task, answers) {
  await writeFile("answers/" + task.questionId + ".json", JSON.stringify({
    question_id: task.questionId,
    session_id: task.sessionId,
    answers: answers,
    answeredAt: now()
  }, null, 2), null, "noriki: answer " + task.questionId);

  task.lane = "done";
  task.doneAt = now();
  render();
  await pushState();
}

/* pull any replies the relay has written */
async function pullOutbox() {
  const entries = await listDir("outbox");
  const fresh = entries.filter((e) => e.type === "file" && e.name.endsWith(".json") && !seen[e.name]);
  if (!fresh.length) return 0;

  for (const e of fresh) {
    try {
      const f = await readFile("outbox/" + e.name);
      if (!f) continue;
      const msg = JSON.parse(f.text);
      const p = state.projects.find((x) => x.id === msg.project);
      if (p) {
        p.thread = p.thread || [];
        p.thread.push({
          role: "bot",
          text: msg.reply || "(empty reply)",
          at: msg.completedAt || now(),
          ms: msg.durationMs,
          ok: msg.ok !== false
        });
      }
      // a reply resolves its pending task
      const t = state.tasks.find((x) => x.msgId === msg.id);
      if (t) { t.lane = "done"; t.doneAt = msg.completedAt || now(); }
      seen[e.name] = true;
    } catch (err) { /* skip a malformed reply rather than stalling the loop */ }
  }
  localStorage.setItem(LS_SEEN, JSON.stringify(seen));
  return fresh.length;
}

async function syncNow(silent) {
  if (syncing || !token || !repo) return;
  syncing = true;
  setSync("working");
  try {
    await pullState();
    const n = await pullOutbox();
    const q = await pullAsks();
    if (n || q) await pushState();
    $("statPull").textContent = new Date().toLocaleTimeString();
    setSync("ok");
    render();
  } catch (e) {
    setSync("error");
    if (!silent) alert("Sync failed: " + e.message);
  } finally {
    syncing = false;
  }
}

/* ---------------- sending a message to the PC ---------------- */

async function sendToRelay(projectId, text) {
  const id = uid();
  const payload = {
    id: id,
    project: projectId,
    cwd: (state.projects.find((p) => p.id === projectId) || {}).path || "",
    prompt: text,
    createdAt: now()
  };

  const p = state.projects.find((x) => x.id === projectId);
  p.thread = p.thread || [];
  p.thread.push({ role: "me", text: text, at: now(), msgId: id });

  state.tasks.push({
    id: uid(),
    msgId: id,
    project: projectId,
    lane: "auto",
    title: text.length > 90 ? text.slice(0, 90) + "…" : text,
    createdAt: now()
  });

  render();

  await writeFile("inbox/" + id + ".json", JSON.stringify(payload, null, 2), null,
                  "noriki: ask " + projectId);
  await pushState();
}

/* ---------------- rendering ---------------- */

function taskCard(t, opts) {
  const p = state.projects.find((x) => x.id === t.project);
  const name = p ? p.name : t.project;
  const running = t.lane === "auto";
  const done = t.lane === "done";

  if (t.kind === "ask" && !done) return askCard(t, name);

  return `
    <div class="task ${done ? "done-row" : ""}">
      <div class="task-top">
        <span class="task-project">${esc(name)}</span>
        <span class="task-age">${esc(ago(t.doneAt || t.createdAt))}</span>
      </div>
      <div class="task-title">${esc(t.title)}</div>
      ${t.why ? `<div class="task-why">${esc(t.why)}</div>` : ""}
      ${running ? `<div class="run-strip"><i></i></div>` : ""}
      ${(!running && !done && opts !== false) ? `
        <div class="task-actions">
          <button class="task-btn primary" data-clear="${esc(t.id)}">Done</button>
          <button class="task-btn" data-defer="${esc(t.id)}">Not today</button>
        </div>` : ""}
    </div>`;
}

/* A blocked session. Answer it and the work moves; ignore it and it doesn't. */
function askCard(t, name) {
  const picked = t.picked || {};
  const qs = t.questions || [];
  const idx = qs.findIndex((q) => !(keyFor(q) in picked));
  const q = idx === -1 ? null : qs[idx];

  const body = q
    ? `<div class="ask-q">${esc(q.question || q.header || "Which way?")}</div>
       <div class="ask-options">
         ${(q.options || []).map((o, i) => `
           <button class="ask-opt" data-ask="${esc(t.id)}" data-qi="${idx}" data-oi="${i}">
             <span class="ask-opt-label">${esc(o.label)}</span>
             ${o.description ? `<span class="ask-opt-desc">${esc(o.description)}</span>` : ""}
           </button>`).join("")}
       </div>
       ${qs.length > 1 ? `<div class="ask-progress">${idx + 1} of ${qs.length}</div>` : ""}`
    : `<div class="ask-q">Sending your answer…</div>`;

  return `
    <div class="task ask ${t.expired ? "expired" : ""}">
      <div class="task-top">
        <span class="task-project">${esc(name)} · blocked</span>
        <span class="task-age">${esc(ago(t.createdAt))}</span>
      </div>
      ${t.expired
        ? `<div class="ask-stale">Stopped waiting — but still answer. Your reply is delivered and the work picks up.</div>`
        : `<div class="ask-live">A session is waiting on you right now.</div>`}
      ${body}
    </div>`;
}

function keyFor(q) { return q.header || q.question || "answer"; }

function renderToday() {
  const today = new Date().toDateString();
  // "Not today" hides a task until tomorrow — that is what the button promises
  const manual = state.tasks.filter((t) => t.lane === "manual" &&
    !(t.deferredAt && new Date(t.deferredAt).toDateString() === today));
  const auto = state.tasks.filter((t) => t.lane === "auto");
  const done = state.tasks.filter((t) => t.lane === "done" &&
    t.doneAt && new Date(t.doneAt).toDateString() === today);

  $("manualList").innerHTML = manual.map((t) => taskCard(t)).join("");
  $("autoList").innerHTML = auto.map((t) => taskCard(t)).join("");
  $("doneList").innerHTML = done.map((t) => taskCard(t, false)).join("");

  $("manualCount").textContent = manual.length;
  $("autoCount").textContent = auto.length;
  $("doneCount").textContent = done.length;

  $("manualEmpty").hidden = manual.length > 0;
  $("autoEmpty").hidden = auto.length > 0;
  $("doneEmpty").hidden = done.length > 0;

  // One check-in per day, and only ever about something still UNCLEARED.
  // Asking "did you do X?" about a task you already marked done is noise.
  const asked = state.checkins.some((c) => new Date(c.at).toDateString() === today);
  const card = $("checkinCard");
  const hour = new Date().getHours();
  const open = state.tasks.filter((t) => t.lane === "manual");
  if (!asked && hour >= 18 && open.length > 0) {
    const target = open[0];
    // Two lines rather than one sentence — task titles are arbitrary text and
    // some are themselves questions, which makes "…hold? Did you?" read badly.
    $("checkinQ").innerHTML =
      `<span class="checkin-said">You said you'd ${esc(lowerFirst(stripEnd(target.title)))}</span>` +
      `<span class="checkin-ask">Did you?</span>`;
    card.dataset.taskId = target.id;
    card.hidden = false;
  } else {
    card.hidden = true;
  }
}

function lowerFirst(s) { return s ? s.charAt(0).toLowerCase() + s.slice(1) : s; }
function stripEnd(s) { return String(s || "").replace(/[.?!\s]+$/, ""); }

function renderProjects() {
  $("projectGrid").innerHTML = state.projects.map((p) => {
    const manual = state.tasks.filter((t) => t.project === p.id && t.lane === "manual").length;
    const auto = state.tasks.filter((t) => t.project === p.id && t.lane === "auto").length;
    // Yours always wins the badge. The whole point of the app is that a thing
    // waiting on you is louder than a thing the machine is already handling.
    let badge = `<span class="pc-badge idle">idle</span>`;
    if (manual) badge = `<span class="pc-badge manual">${manual} yours</span>`;
    else if (auto) badge = `<span class="pc-badge auto">${auto} running</span>`;

    const days = p.due
      ? Math.ceil((new Date(p.due) - Date.now()) / 86400000)
      : null;

    return `
      <button class="project-card" data-project="${esc(p.id)}">
        <div class="pc-top">
          <span class="pc-name">${esc(p.name)}</span>
          ${badge}
        </div>
        <div class="pc-kind">${esc(p.kind)}${days !== null ? ` · ${days} days left` : ""}</div>
        ${p.next ? `<div class="pc-next">${esc(p.next)}</div>` : ""}
        <div class="pc-path">${esc(p.path)}</div>
      </button>`;
  }).join("");
}

function renderChat() {
  const p = state.projects.find((x) => x.id === currentProject);
  if (!p) return;
  const thread = p.thread || [];
  $("chatLog").innerHTML = thread.map((m) => {
    if (m.role === "me") {
      const answered = thread.some((r) => r.role === "bot" && r.at > m.at);
      return `<div class="msg ${answered ? "me" : "pending"}">${esc(m.text)}
        ${answered ? "" : `<div class="msg-meta">waiting for your PC…</div>`}</div>`;
    }
    return `<div class="msg bot">${esc(m.text)}
      <div class="msg-meta">${esc(ago(m.at))}${m.ms ? " · " + Math.round(m.ms / 1000) + "s" : ""}</div></div>`;
  }).join("");
  $("chatLog").scrollTop = $("chatLog").scrollHeight;
}

function renderCapture() {
  const items = (state.captures || []).slice().reverse().slice(0, 40);
  $("captureList").innerHTML = items.map((c) =>
    `<div class="capture-item">${esc(c.text)}<time>${esc(new Date(c.at).toLocaleString())}</time></div>`
  ).join("");
}

function render() {
  if (!state) return;
  renderToday();
  renderProjects();
  renderCapture();
  if (currentProject) renderChat();
  $("statPending").textContent = state.tasks.filter((t) => t.lane === "auto").length;
}

/* ---------------- navigation ---------------- */

const VIEWS = ["today", "projects", "chat", "capture", "settings"];

function show(view, opts) {
  VIEWS.forEach((v) => {
    const el = $("view" + v.charAt(0).toUpperCase() + v.slice(1));
    if (el) el.hidden = v !== view;
  });
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.view === view));

  const backable = view === "chat" || view === "settings";
  $("navBack").hidden = !backable;

  if (view === "chat" && currentProject) {
    const p = state.projects.find((x) => x.id === currentProject);
    $("topTitle").textContent = p ? p.name : "Chat";
    $("topSub").textContent = p ? p.path : "";
    renderChat();
  } else {
    $("topTitle").textContent =
      view === "today" ? "Today" :
      view === "projects" ? "Projects" :
      view === "capture" ? "Capture" : "Settings";
    $("topSub").textContent = "";
  }
}

/* ---------------- wiring ---------------- */

function wire() {
  document.querySelectorAll(".tab").forEach((t) =>
    t.addEventListener("click", () => { currentProject = null; show(t.dataset.view); }));

  $("navSettings").addEventListener("click", () => show("settings"));
  $("navBack").addEventListener("click", () => { currentProject = null; show("projects"); });

  // project → chat
  $("projectGrid").addEventListener("click", (e) => {
    const card = e.target.closest("[data-project]");
    if (!card) return;
    currentProject = card.dataset.project;
    show("chat");
  });

  // answering a blocked session
  $("viewToday").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-ask]");
    if (!btn) return;
    const t = state.tasks.find((x) => x.id === btn.dataset.ask);
    if (!t) return;

    const q = (t.questions || [])[Number(btn.dataset.qi)];
    const opt = (q.options || [])[Number(btn.dataset.oi)];
    if (!q || !opt) return;

    t.picked = t.picked || {};
    t.picked[keyFor(q)] = opt.label;
    render();

    // only send once every question has an answer
    const complete = (t.questions || []).every((qq) => keyFor(qq) in t.picked);
    if (!complete) return;

    setSync("working");
    try {
      await sendAnswer(t, t.picked);
      setSync("ok");
    } catch (err) {
      setSync("error");
      alert("Couldn't send the answer: " + err.message);
    }
  });

  // task buttons
  $("viewToday").addEventListener("click", async (e) => {
    const clear = e.target.closest("[data-clear]");
    const defer = e.target.closest("[data-defer]");
    if (!clear && !defer) return;
    const id = (clear || defer).dataset.clear || (clear || defer).dataset.defer;
    const t = state.tasks.find((x) => x.id === id);
    if (!t) return;
    if (clear) { t.lane = "done"; t.doneAt = now(); }
    else { t.deferredAt = now(); }          // stays manual, hidden until tomorrow
    render();
    try { await pushState(); } catch (err) { alert(err.message); }
  });

  // check-in
  document.querySelectorAll("[data-answer]").forEach((b) =>
    b.addEventListener("click", async () => {
      const card = $("checkinCard");
      state.checkins.push({
        at: now(),
        taskId: card.dataset.taskId,
        answer: b.dataset.answer
      });
      card.hidden = true;
      render();
      try { await pushState(); } catch (err) { alert(err.message); }
    }));

  // chat send
  const input = $("chatInput");
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 132) + "px";
    $("chatSend").disabled = !input.value.trim();
  });
  $("chatSend").disabled = true;

  $("chatSend").addEventListener("click", async () => {
    const text = input.value.trim();
    if (!text || !currentProject) return;
    input.value = "";
    input.style.height = "auto";
    $("chatSend").disabled = true;
    setSync("working");
    try {
      await sendToRelay(currentProject, text);
      setSync("ok");
    } catch (e) {
      setSync("error");
      alert("Couldn't send: " + e.message);
    }
  });

  // capture
  $("captureSave").addEventListener("click", async () => {
    const text = $("captureInput").value.trim();
    if (!text) return;
    state.captures = state.captures || [];
    state.captures.push({ at: now(), text: text });
    $("captureInput").value = "";
    render();
    try { await pushState(); } catch (e) { alert(e.message); }
  });

  // settings
  $("setSave").addEventListener("click", () => connect($("setRepo").value, $("setToken").value, "setStatus"));
  $("syncNow").addEventListener("click", () => syncNow(false));
  $("wipeLocal").addEventListener("click", () => {
    if (!confirm("Remove the token from this device? Your data stays in the repo.")) return;
    localStorage.removeItem(LS_TOKEN);
    location.reload();
  });

  $("obGo").addEventListener("click", () => connect($("obRepo").value, $("obToken").value, "obStatus"));
}

async function connect(repoVal, tokenVal, statusId) {
  const status = $(statusId);
  repoVal = (repoVal || "").trim().replace(/^https?:\/\/github\.com\//, "").replace(/\.git$/, "");
  tokenVal = (tokenVal || "").trim();
  if (!repoVal || !tokenVal) {
    status.className = "set-status err";
    status.textContent = "Both fields are required.";
    return;
  }
  status.className = "set-status";
  status.textContent = "Connecting…";

  const prevRepo = repo, prevToken = token;
  repo = repoVal; token = tokenVal;
  try {
    const info = await gh(`/repos/${repo}`);
    if (!info) throw new Error("Repo not found, or the token can't see it.");
    localStorage.setItem(LS_REPO, repo);
    localStorage.setItem(LS_TOKEN, token);
    status.className = "set-status ok";
    status.textContent = "Connected to " + repo + (info.private ? " (private)" : " — WARNING: this repo is public");
    $("onboard").hidden = true;
    $("app").hidden = false;
    await boot();
  } catch (e) {
    repo = prevRepo; token = prevToken;
    status.className = "set-status err";
    status.textContent = e.message;
  }
}

/* ---------------- boot ---------------- */

async function boot() {
  $("setRepo").value = repo;
  $("setToken").value = token ? "••••••••••••" : "";
  $("buildVer").textContent = BUILD;

  const cached = localStorage.getItem(LS_CACHE);
  if (cached) {
    try { state = JSON.parse(cached).state; render(); } catch (e) {}
  }

  await syncNow(true);
  show("today");

  clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if (document.visibilityState === "visible") syncNow(true);
  }, POLL_MS);
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") syncNow(true);
});

wire();

if (token && repo) {
  $("onboard").hidden = true;
  $("app").hidden = false;
  boot();
} else {
  $("onboard").hidden = false;
}
