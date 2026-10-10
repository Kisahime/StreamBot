// Read-only OBS page. It listens on the WebSocket and never sends the booth control token.
const phaseEl = document.getElementById("phase");
const lineEl = document.getElementById("line");
const nameEl = document.getElementById("name");
const alertEl = document.getElementById("alert");
const connEl = document.getElementById("conn");
const q = new URLSearchParams(location.search);
document.body.dataset.theme = q.get("theme") || "";
document.body.dataset.position = q.get("position") || "bottom";
const hideIdle = q.get("hide") === "idle";
const alertsOff = q.get("alerts") === "0";
const hypeOff = q.get("hype") === "0";
const askOff = q.get("ask") === "0";
const hypeEl = document.getElementById("hype");
const hypeBar = document.getElementById("hypeBar");
const hypeLabel = document.getElementById("hypeLabel");
const adEl = document.getElementById("ad");
const questionEl = document.getElementById("question");
let holdMs = Math.max(1, Number(q.get("hold") || 6)) * 1000;
let alertTimer = 0;
let typeTimer = 0;
let hypeTimer = 0;
let hypeTick = 0;
let adTimer = 0;
const fontSize = Number(q.get("fontsize"));
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

if (hideIdle) document.body.classList.add("hide-idle");
if (alertsOff) document.body.classList.add("alerts-off");
if (fontSize > 0 && lineEl) lineEl.style.fontSize = `${fontSize}px`;

function setLive(phase) {
  const live = phase && phase !== "idle";
  document.body.classList.toggle("live", live || !hideIdle);
}

async function boot() {
  try {
    const health = await fetch("/api/health").then((r) => r.json());
    if (health.persona) nameEl.textContent = health.persona;
    const overlay = health.overlay || {};
    if (!q.get("hold") && overlay.alert_hold_seconds) {
      holdMs = Math.max(1, Number(overlay.alert_hold_seconds)) * 1000;
    }
  } catch {
    /* overlay still works via ws */
  }
}

function showAlert(text, kind) {
  if (!alertEl || !text || alertsOff) return;
  alertEl.hidden = false;
  alertEl.dataset.kind = kind || "alert";
  alertEl.textContent = text;
  clearTimeout(alertTimer);
  alertTimer = setTimeout(() => {
    alertEl.hidden = true;
  }, holdMs);
}

function setCaption(text, speaking) {
  if (!lineEl) return;
  const next = text || "";
  if (!speaking || reduceMotion.matches) {
    clearInterval(typeTimer);
    lineEl.textContent = next;
    return;
  }
  if (lineEl.dataset.full === next) return;
  lineEl.dataset.full = next;
  clearInterval(typeTimer);
  let index = 0;
  lineEl.textContent = "";
  typeTimer = setInterval(() => {
    index += 1;
    lineEl.textContent = next.slice(0, index);
    if (index >= next.length) clearInterval(typeTimer);
  }, 18);
}

function hideHype() {
  if (hypeEl) hypeEl.hidden = true;
  clearInterval(hypeTick);
}

function showHype(data) {
  if (hypeOff || !hypeEl || !data) return;
  const hold = Math.max(1, Number(data.hold_seconds || holdMs / 1000)) * 1000;
  if (data.active === false) {
    clearTimeout(hypeTimer);
    hypeTimer = setTimeout(hideHype, hold);
    return;
  }
  clearTimeout(hypeTimer);
  hypeEl.hidden = false;
  const progress = Number(data.progress) || 0;
  const goal = Number(data.goal) || 0;
  const pct = goal > 0 ? Math.max(0, Math.min(100, (progress / goal) * 100)) : 0;
  if (hypeBar) hypeBar.style.width = `${pct}%`;
  const level = data.level || 1;
  const tick = () => {
    let left = "";
    if (data.expires_at) {
      const end = Date.parse(data.expires_at);
      if (!Number.isNaN(end)) left = ` · ${Math.max(0, Math.round((end - Date.now()) / 1000))}s`;
    }
    if (hypeLabel) hypeLabel.textContent = `Hype level ${level}${left}`;
  };
  tick();
  clearInterval(hypeTick);
  hypeTick = setInterval(tick, 1000);
}

function showAd(seconds) {
  if (!adEl) return;
  let left = Math.max(0, Number(seconds) || 0);
  adEl.hidden = false;
  const tick = () => {
    adEl.textContent = `Ad break, back in ${left} seconds`;
    if (left <= 0) {
      clearInterval(adTimer);
      return;
    }
    left -= 1;
  };
  tick();
  clearInterval(adTimer);
  adTimer = setInterval(tick, 1000);
}

function showQuestion(data) {
  if (askOff || !questionEl) return;
  const text = (data && data.text) || "";
  if (!text) {
    questionEl.hidden = true;
    questionEl.textContent = "";
    return;
  }
  questionEl.hidden = false;
  const user = (data && data.user) || "";
  questionEl.textContent = user ? `${user}: ${text}` : text;
}

function applyOverlayMessage(raw) {
  let msg;
  try {
    msg = JSON.parse(raw);
  } catch {
    return;
  }
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "status") {
    const st = msg.data || {};
    const phase = st.phase || "idle";
    phaseEl.textContent = phase;
    phaseEl.className = "pill " + phase;
    setLive(phase);
    if (st.persona) nameEl.textContent = st.persona;
    if (st.speaking) nameEl.classList.add("speaking");
    else nameEl.classList.remove("speaking");
    if (typeof st.caption === "string") setCaption(st.caption, !!st.speaking);
    if (st.hype) showHype(st.hype);
    if (st.queue && st.queue.questions) showQuestion(st.queue.questions[0] || {});
  } else if (msg.type === "hype") {
    showHype(msg.data || {});
  } else if (msg.type === "question") {
    showQuestion(msg.data || {});
  } else if (msg.type === "transcript" && msg.data && msg.data.role === "cohost") {
    setCaption(msg.data.text || "", true);
  } else if (msg.type === "alert") {
    const d = msg.data || {};
    showAlert(d.text || `${d.kind || "alert"} ${d.user || ""}`.trim(), d.kind || "alert");
    if (d.kind === "ad_break") {
      const match = /(\d+)/.exec(d.text || "");
      showAd(match ? Number(match[1]) : 0);
    }
  }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => connEl.classList.remove("off");
  ws.onmessage = (ev) => applyOverlayMessage(ev.data);
  ws.onclose = () => {
    connEl.classList.add("off");
    setTimeout(connect, 1500);
  };
}

window.applyOverlayMessage = applyOverlayMessage;
boot();
connect();
