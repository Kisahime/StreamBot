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
let holdMs = Math.max(1, Number(q.get("hold") || 6)) * 1000;
let alertTimer = 0;
let typeTimer = 0;
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
  } else if (msg.type === "transcript" && msg.data && msg.data.role === "cohost") {
    setCaption(msg.data.text || "", true);
  } else if (msg.type === "alert") {
    const d = msg.data || {};
    showAlert(d.text || `${d.kind || "alert"} ${d.user || ""}`.trim(), d.kind || "alert");
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
