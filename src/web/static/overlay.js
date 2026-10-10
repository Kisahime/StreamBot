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
const holdMs = Math.max(1, Number(q.get("hold") || 6)) * 1000;
let alertTimer = 0;

if (hideIdle) document.body.classList.add("hide-idle");

function setLive(phase) {
  const live = phase && phase !== "idle";
  document.body.classList.toggle("live", live || !hideIdle);
}

async function boot() {
  try {
    const health = await fetch("/api/health").then((r) => r.json());
    if (health.persona) nameEl.textContent = health.persona;
  } catch {
    /* overlay still works via ws */
  }
}

function showAlert(text) {
  if (!alertEl || !text) return;
  alertEl.hidden = false;
  alertEl.textContent = text;
  clearTimeout(alertTimer);
  alertTimer = setTimeout(() => {
    alertEl.hidden = true;
  }, holdMs);
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => connEl.classList.remove("off");
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "status") {
      const st = msg.data || {};
      const phase = st.phase || "idle";
      phaseEl.textContent = phase;
      phaseEl.className = "pill " + phase;
      setLive(phase);
      if (st.persona) nameEl.textContent = st.persona;
      if (st.speaking) nameEl.classList.add("speaking");
      else nameEl.classList.remove("speaking");
      if (st.caption) lineEl.textContent = st.caption;
    } else if (msg.type === "transcript" && msg.data && msg.data.role === "cohost") {
      lineEl.textContent = msg.data.text || "";
    } else if (msg.type === "alert") {
      const d = msg.data || {};
      showAlert(d.text || `${d.kind || "alert"} ${d.user || ""}`.trim());
    }
  };
  ws.onclose = () => {
    connEl.classList.add("off");
    setTimeout(connect, 1500);
  };
}

boot();
connect();
