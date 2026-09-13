const $ = (id) => document.getElementById(id);

const listenBtn = $("listenBtn");
const pttBtn = $("pttBtn");
const muteBtn = $("muteBtn");
const stopBtn = $("stopBtn");
const twitchBtn = $("twitchBtn");
const personaBtn = $("personaBtn");
const clearMemBtn = $("clearMemBtn");
const phasePill = $("phasePill");
const hint = $("hint");
const sysList = $("sysList");
const lastUser = $("lastUser");
const caption = $("caption");
const chatLog = $("chatLog");
const eventLog = $("eventLog");
const memLog = $("memLog");

let listenOn = false;
let muted = false;
let twitchState = "disconnected";
let pttHeld = false;

function logLine(target, html) {
  const p = document.createElement("p");
  p.innerHTML = html;
  target.prepend(p);
  while (target.children.length > 80) target.removeChild(target.lastChild);
}

function setPhase(phase) {
  phasePill.textContent = phase || "idle";
  phasePill.className = "phase " + (phase || "idle");
}

async function defPost(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

listenBtn.addEventListener("click", async () => {
  listenOn = !listenOn;
  await defPost("/api/listen", { enabled: listenOn });
  listenBtn.textContent = listenOn ? "Stop listening" : "Start listening";
  listenBtn.classList.toggle("active", listenOn);
});

function ptt(down) {
  if (pttHeld === down) return;
  pttHeld = down;
  pttBtn.classList.toggle("held", down);
  defPost("/api/ptt", { down }).catch((e) => logLine(eventLog, `<span class="warn">${e}</span>`));
}
pttBtn.addEventListener("mousedown", () => ptt(true));
pttBtn.addEventListener("mouseup", () => ptt(false));
pttBtn.addEventListener("mouseleave", () => ptt(false));
pttBtn.addEventListener("touchstart", (e) => { e.preventDefault(); ptt(true); }, { passive: false });
pttBtn.addEventListener("touchend", () => ptt(false));
window.addEventListener("keydown", (e) => {
  if (e.code !== "Space" || e.repeat) return;
  const tag = (e.target && e.target.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA") return;
  e.preventDefault();
  ptt(true);
});
window.addEventListener("keyup", (e) => {
  if (e.code === "Space") ptt(false);
});

muteBtn.addEventListener("click", async () => {
  muted = !muted;
  await defPost("/api/tts/mute", { mute: muted });
  muteBtn.textContent = muted ? "Unmute voice" : "Mute voice";
  muteBtn.classList.toggle("active", muted);
});

stopBtn.addEventListener("click", () => defPost("/api/interrupt", {}));

twitchBtn.addEventListener("click", async () => {
  try {
    if (twitchState === "connected" || twitchState === "connecting") {
      await defPost("/api/twitch/disconnect", {});
    } else {
      await defPost("/api/twitch/connect", {});
    }
  } catch (e) {
    hint.textContent = String(e.message || e);
    logLine(eventLog, `<span class="warn">${e}</span>`);
  }
});

personaBtn.addEventListener("click", async () => {
  const data = await defPost("/api/persona/reload", {});
  hint.textContent = `Persona reloaded: ${data.name || "ok"}`;
});

clearMemBtn.addEventListener("click", async () => {
  await defPost("/api/memory/clear", {});
  memLog.innerHTML = "";
  hint.textContent = "Conversation memory cleared.";
});

function renderMemory(snap) {
  if (!snap || !memLog) return;
  memLog.innerHTML = "";
  const events = snap.events || [];
  for (const line of events.slice().reverse()) {
    logLine(memLog, `<span class="mem">${line}</span>`);
  }
}

function renderSys(health) {
  const gpu = health.gpu || {};
  const ollama = health.ollama || {};
  const whisper = health.whisper || {};
  const tts = health.tts || {};
  const twitch = health.twitch || {};
  const rows = [
    ["GPU", gpu.ok ? `${gpu.name} · ${gpu.memory_used_mb}/${gpu.memory_total_mb} MB` : (gpu.error || "not found")],
    ["Ollama", ollama.ok ? `${ollama.model}${ollama.has_model ? "" : " (pull needed)"}` : (ollama.error || "down")],
    ["Whisper", whisper.loaded ? `${whisper.model} @ ${whisper.device}` : (whisper.error || "not loaded")],
    ["Piper TTS", tts.ready ? "ready" : (tts.error || "not ready")],
    ["Twitch", `${twitch.state || "disconnected"} · #${twitch.channel || "?"} as ${twitch.nick || "?"}`],
    ["Persona", health.persona || "—"],
  ];
  if (sysList) sysList.innerHTML = rows.map(([k, v]) => `<li><strong>${k}</strong><span>${v}</span></li>`).join("");
  const hints = health.setup || [];
  if (hints.length && hint) hint.textContent = hints[0];
  const deviceList = $("deviceList");
  if (!deviceList) return;
  const devices = health.devices || {};
  if (devices.error) {
    deviceList.textContent = devices.error;
  } else {
    const ins = (devices.inputs || []).slice(0, 8).map((d) => `#${d.index} ${d.name}`).join("\n") || "(none)";
    const outs = (devices.outputs || []).slice(0, 8).map((d) => `#${d.index} ${d.name}`).join("\n") || "(none)";
    deviceList.textContent =
      `Mic default: ${devices.default_input}\n${ins}\n\nSpeaker default: ${devices.default_output}\n${outs}`;
  }
}

async function refreshHealth() {
  try {
    const res = await fetch("/api/health", { cache: "no-store" });
    if (!res.ok) return;
    const health = await res.json();
    renderSys(health);
    const st = health.status || {};
    setPhase(st.phase);
    if (st.last_user) lastUser.textContent = "You: " + st.last_user;
    if (st.caption) caption.textContent = st.caption;
    listenOn = !!st.listen;
    muted = !!st.tts_mute;
    twitchState = st.twitch || "disconnected";
    listenBtn.textContent = listenOn ? "Stop listening" : "Start listening";
    listenBtn.classList.toggle("active", listenOn);
    muteBtn.textContent = muted ? "Unmute voice" : "Mute voice";
    muteBtn.classList.toggle("active", muted);
    twitchBtn.textContent = twitchState === "connected" ? "Disconnect Twitch" : "Connect Twitch";
    if (health.memory) renderMemory(health.memory);
  } catch (e) {
    console.warn("health poll failed", e);
  }
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "status") {
      const st = msg.data || {};
      setPhase(st.phase);
      if (st.last_user) lastUser.textContent = "You: " + st.last_user;
      if (typeof st.caption === "string" && st.caption) caption.textContent = st.caption;
      listenOn = !!st.listen;
      muted = !!st.tts_mute;
      twitchState = st.twitch || twitchState;
      twitchBtn.textContent = twitchState === "connected" ? "Disconnect Twitch" : "Connect Twitch";
    } else if (msg.type === "chat") {
      const d = msg.data || {};
      const cls = d.bot ? "bot" : "u";
      logLine(chatLog, `<span class="${cls}">${d.user || "Pixel"}</span> ${d.text}`);
    } else if (msg.type === "transcript") {
      const d = msg.data || {};
      logLine(eventLog, `<span class="u">${d.role}</span> ${d.text}`);
      if (d.role === "cohost") caption.textContent = d.text;
      if (d.role === "streamer") lastUser.textContent = "You: " + d.text;
    } else if (msg.type === "memory") {
      renderMemory(msg.data);
    } else if (msg.type === "log") {
      logLine(eventLog, `<span class="warn">${msg.data}</span>`);
    }
  };
  ws.onclose = () => setTimeout(connectWs, 1500);
}

refreshHealth();
setInterval(refreshHealth, 8000);
connectWs();
