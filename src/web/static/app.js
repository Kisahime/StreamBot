const $ = (id) => document.getElementById(id);

const listenBtn = $("listenBtn");
const pttBtn = $("pttBtn");
const muteBtn = $("muteBtn");
const stopBtn = $("stopBtn");
const twitchBtn = $("twitchBtn");
const eventBtn = $("eventBtn");
const personaBtn = $("personaBtn");
const personaSave = $("personaSave");
const clearMemBtn = $("clearMemBtn");
const saveStreamerBtn = $("saveStreamerBtn");
const streamerName = $("streamerName");
const streamerLogin = $("streamerLogin");
const peopleList = $("peopleList");
const phasePill = $("phasePill");
const hint = $("hint");
const sysList = $("sysList");
const feedList = $("feedList");
const lastUser = $("lastUser");
const caption = $("caption");
const chatLog = $("chatLog");
const eventLog = $("eventLog");
const alertLog = $("alertLog");
const memLog = $("memLog");
const queueLog = $("queueLog");
const meter = $("meter");

let listenOn = false;
let muted = false;
let twitchState = "disconnected";
let eventState = "disconnected";
let pttHeld = false;
let streamerLabel = "Streamer";
let selectedPerson = "streamer";
let lastPeople = [];
let devicesLoaded = false;
let personaName = "Cohost";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function logLine(target, text, className) {
  if (!target) return;
  const p = document.createElement("p");
  if (className) {
    p.appendChild(el("span", className, text));
  } else {
    p.textContent = text;
  }
  target.prepend(p);
  while (target.children.length > 80) target.removeChild(target.lastChild);
}

function logParts(target, parts) {
  if (!target) return;
  const p = document.createElement("p");
  for (const part of parts) {
    if (part.cls) p.appendChild(el("span", part.cls, part.text));
    else p.appendChild(document.createTextNode(part.text));
  }
  target.prepend(p);
  while (target.children.length > 80) target.removeChild(target.lastChild);
}

function setPhase(phase) {
  phasePill.textContent = phase || "idle";
  phasePill.className = "phase " + (phase || "idle") + (phase === "speaking" ? " speaking-pulse" : "");
}

let controlToken = "";
const sessionReady = (async () => {
  try {
    const res = await fetch("/api/session", { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    controlToken = data.token || "";
  } catch {
    /* health can still render if the session call fails */
  }
})();

async function defPost(url, body) {
  await sessionReady;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Cohost-Token": controlToken,
    },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

async function post(url, body) {
  try {
    return await defPost(url, body);
  } catch (e) {
    const message = String((e && e.message) || e);
    if (hint) hint.textContent = message;
    return { ok: false, error: message };
  }
}

function failed(data) {
  return data && data.ok === false;
}

function typingInField() {
  const tag = (document.activeElement && document.activeElement.tagName) || "";
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

listenBtn.addEventListener("click", async () => {
  try {
    await post("/api/listen", { enabled: !listenOn });
  } catch (e) {
    hint.textContent = String(e.message || e);
  }
});

function ptt(down) {
  if (pttHeld === down) return;
  pttHeld = down;
  pttBtn.classList.toggle("held", down);
  post("/api/ptt", { down }).catch((e) => logLine(eventLog, String(e.message || e), "warn"));
}
pttBtn.addEventListener("mousedown", () => ptt(true));
pttBtn.addEventListener("mouseup", () => ptt(false));
pttBtn.addEventListener("mouseleave", () => ptt(false));
pttBtn.addEventListener("touchstart", (e) => { e.preventDefault(); ptt(true); }, { passive: false });
pttBtn.addEventListener("touchend", () => ptt(false));
window.addEventListener("keydown", (e) => {
  if (typingInField() || e.repeat) return;
  if (e.code === "Space") {
    e.preventDefault();
    ptt(true);
  } else if (e.key === "m" || e.key === "M") {
    muteBtn.click();
  } else if (e.key === "Escape") {
    stopBtn.click();
  } else if (e.key === "l" || e.key === "L") {
    listenBtn.click();
  }
});
window.addEventListener("keyup", (e) => {
  if (e.code === "Space" && !typingInField()) ptt(false);
});

muteBtn.addEventListener("click", async () => {
  await post("/api/tts/mute", { mute: !muted });
});
stopBtn.addEventListener("click", () => post("/api/interrupt", {}));

twitchBtn.addEventListener("click", async () => {
  const on = twitchState === "connected" || twitchState === "connecting" || twitchState === "reconnecting";
  const data = await post("/api/twitch/irc", { enabled: !on });
  if (failed(data)) logLine(eventLog, data.error, "warn");
});

eventBtn.addEventListener("click", async () => {
  const on = eventState === "connected" || eventState === "connecting" || eventState === "reconnecting";
  const data = await post("/api/twitch/events", { enabled: !on });
  if (failed(data)) logLine(eventLog, data.error, "warn");
});

$("shoutoutBtn").addEventListener("click", async () => {
  const data = await post("/api/twitch/shoutout", {});
  if (failed(data)) return;
  hint.textContent = "Shoutout sent.";
});

const debugLogBtn = $("debugLogBtn");
if (debugLogBtn) {
  debugLogBtn.addEventListener("click", async () => {
    const data = await post("/api/logging/debug", { enabled: !debugLogBtn.classList.contains("active") });
    if (failed(data)) return;
    debugLogBtn.classList.toggle("active", !!data.debug);
    hint.textContent = data.debug ? "Debug log on for 10 minutes." : "Debug log off.";
  });
}

$("markerBtn").addEventListener("click", async () => {
  const data = await post("/api/marker", { note: "clip moment" });
  if (failed(data)) return;
  hint.textContent = data.marker_error ? `Noted. Marker: ${data.marker_error}` : "Moment noted.";
});

personaBtn.addEventListener("click", async () => {
  const data = await post("/api/persona/reload", {});
  if (failed(data)) return;
  hint.textContent = `Persona reloaded: ${data.name || "ok"}`;
  await loadPersona();
});

personaSave.addEventListener("click", async () => {
  const data = await post("/api/persona", { text: $("personaText").value });
  if (failed(data)) return;
  hint.textContent = `Persona saved: ${data.name || "ok"}`;
});

clearMemBtn.addEventListener("click", async () => {
  const data = await post("/api/memory/clear", {});
  if (failed(data)) return;
  if (memLog) memLog.textContent = "";
  if (peopleList) peopleList.textContent = "";
  hint.textContent = "Conversation memory cleared.";
});

saveStreamerBtn.addEventListener("click", async () => {
  const data = await post("/api/streamer", {
    display_name: streamerName.value.trim(),
    twitch_login: streamerLogin.value.trim(),
  });
  if (failed(data)) return;
  streamerLabel = data.spoken_name || streamerName.value.trim() || "Streamer";
  hint.textContent = `${personaName} will treat ${streamerLabel} as the streamer.`;
});

$("mood").addEventListener("change", async () => {
  try {
    await post("/api/mood", { mood: $("mood").value });
  } catch (e) {
    hint.textContent = String(e.message || e);
  }
});

$("postVoice").addEventListener("change", () => {
  post("/api/chat/post_voice", { enabled: $("postVoice").checked }).catch((e) => {
    hint.textContent = String(e.message || e);
  });
});

$("sayBtn").addEventListener("click", () => speakBox(false));
$("sayChatBtn").addEventListener("click", () => speakBox(true));
async function speakBox(postChat) {
  const text = $("sayText").value.trim();
  if (!text) return;
  const data = await post("/api/say", { text, post_chat: postChat });
  if (failed(data)) return;
  $("sayText").value = "";
}
$("pretendBtn").addEventListener("click", async () => {
  const text = $("pretendText").value.trim();
  if (!text) return;
  const data = await post("/api/pretend", { text });
  if (failed(data)) return;
  $("pretendText").value = "";
});

function youLine(text) {
  return `You (${streamerLabel}): ${text}`;
}

function renderPeople(snap) {
  lastPeople = (snap && snap.people) || [];
  if (!peopleList) return;
  peopleList.textContent = "";
  if (!lastPeople.length) {
    peopleList.appendChild(el("p", "hint", "No conversations yet."));
    return;
  }
  for (const p of lastPeople) {
    const row = document.createElement("div");
    row.className = "person-row";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "person" + ((p.id || "") === selectedPerson ? " active" : "");
    if (p.is_streamer) btn.appendChild(el("span", "tag", "streamer "));
    btn.appendChild(el("strong", "", p.name || ""));
    const last = (p.last_said || p.last_reply || "").slice(0, 90);
    btn.appendChild(el("span", "", last || "—"));
    btn.addEventListener("click", () => {
      selectedPerson = p.id || "streamer";
      renderThread(p);
      renderPeople(snap);
    });
    row.appendChild(btn);
    if (!p.is_streamer) {
      const forget = document.createElement("button");
      forget.type = "button";
      forget.className = "forget";
      forget.textContent = "Forget";
      forget.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        const data = await post("/api/memory/forget", { login: p.name || "" });
        if (failed(data)) return;
        lastPeople = lastPeople.filter((item) => item.id !== p.id);
        if (selectedPerson === p.id) selectedPerson = "streamer";
        renderPeople({ people: lastPeople });
      });
      row.appendChild(forget);
    }
    peopleList.appendChild(row);
  }
}

function renderThread(person) {
  if (!memLog) return;
  memLog.textContent = "";
  const lines = (person && person.thread) || [];
  for (const line of lines.slice().reverse()) logLine(memLog, line, "mem");
}

function renderMemory(snap) {
  if (!snap) return;
  renderPeople(snap);
  const person = lastPeople.find((p) => p.id === selectedPerson) || lastPeople[0];
  if (person) selectedPerson = person.id;
  renderThread(person);
}

function fillSelect(select, options, current, emptyLabel) {
  const value = select.value;
  select.textContent = "";
  if (emptyLabel != null) {
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = emptyLabel;
    select.appendChild(blank);
  }
  for (const opt of options) {
    const node = document.createElement("option");
    node.value = String(opt.value);
    node.textContent = opt.label;
    if (opt.name) node.dataset.name = opt.name;
    select.appendChild(node);
  }
  const want = current == null ? "" : String(current);
  select.value = [...select.options].some((o) => o.value === want) ? want : (value || "");
}

function renderSys(health) {
  if (health.persona) personaName = health.persona;
  const gpu = health.gpu || {};
  const ollama = health.ollama || {};
  const whisper = health.whisper || {};
  const tts = health.tts || {};
  const twitch = health.twitch || {};
  const vram = health.vram || {};
  const rows = [
    ["GPU", gpu.ok ? `${gpu.name} · ${gpu.memory_used_mb}/${gpu.memory_total_mb} MB` : (gpu.error || "not found")],
    ["VRAM", `${vram.profile || "snappy"} · warn ${vram.warn_mb || "?"} MB`],
    ["Ollama", ollama.ok ? `${ollama.model}${ollama.has_model ? "" : " (pull needed)"}` : (ollama.error || "down")],
    ["Whisper", whisper.loaded ? `${whisper.model} @ ${whisper.device}` : (whisper.error || "not loaded")],
    ["Piper TTS", tts.ready ? `ready · ${tts.mode || "oneshot"}` : (tts.error || "not ready")],
    ["Duck", (health.duck && health.duck.mode) || "off"],
    ["Wake", health.wake && health.wake.enabled ? (health.wake.ready ? "ready" : (health.wake.reason || "off")) : "off"],
  ];
  if (sysList) {
    sysList.textContent = "";
    for (const [k, v] of rows) {
      const li = document.createElement("li");
      li.appendChild(el("strong", "", k));
      li.appendChild(el("span", "", v));
      sysList.appendChild(li);
    }
  }
  const hints = health.setup || [];
  if (hints.length && hint && !hint.dataset.lock) hint.textContent = hints[0];
  $("ircLine").textContent = `IRC: ${twitch.state || "disconnected"} · #${twitch.channel || "?"} as ${twitch.nick || "?"}`;
  $("eventLine").textContent = `EventSub: ${twitch.eventsub || "disconnected"}`;
  if (feedList) {
    feedList.textContent = "";
    const feeds = twitch.feeds || {};
    for (const name of Object.keys(feeds)) {
      feedList.appendChild(el("li", "", `${name}: ${feeds[name]}`));
    }
  }
  const actions = twitch.actions || {};
  for (const [id, key] of [["shoutoutBtn", "shoutout"], ["markerBtn", "marker"]]) {
    const btn = $(id);
    const state = String(actions[key] || "");
    const missing = state.startsWith("missing scope");
    if (!btn) continue;
    btn.disabled = missing;
    btn.title = missing ? state : "";
  }
  const timings = health.timings || {};
  const bits = [];
  if (timings.stt_ms != null) bits.push(`STT ${timings.stt_ms} ms`);
  if (timings.first_token_ms != null) bits.push(`token ${timings.first_token_ms} ms`);
  if (timings.first_audio_ms != null) bits.push(`audio ${timings.first_audio_ms} ms`);
  if (timings.chat_send_ms != null) bits.push(`chat ${timings.chat_send_ms} ms`);
  $("timingLine").textContent = bits.join(" · ");
  if (health.mood && document.activeElement !== $("mood")) $("mood").value = health.mood;
  const profileSelect = $("profileSelect");
  if (profileSelect) {
    const names = health.profiles || [];
    const sig = names.join("|");
    if (profileSelect.dataset.names !== sig) {
      profileSelect.dataset.names = sig;
      profileSelect.textContent = "";
      const blank = document.createElement("option");
      blank.value = "";
      blank.textContent = "config defaults";
      profileSelect.appendChild(blank);
      for (const name of names) {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        profileSelect.appendChild(opt);
      }
    }
    if (document.activeElement !== profileSelect) profileSelect.value = health.profile || "";
  }
  const runtime = health.runtime || {};
  const profileLine = $("profileLine");
  if (profileLine && runtime.mood) {
    const spoken = runtime.events_speak ? "spoken" : "typed";
    profileLine.textContent = `mood ${runtime.mood} · ${runtime.max_replies_per_minute} replies/min · cooldown ${runtime.cooldown_seconds}s · events ${spoken}`;
  }
  if (health.language && document.activeElement !== $("langSelect")) $("langSelect").value = health.language;
  if (!devicesLoaded && health.devices) {
    const devices = health.devices;
    applyDeviceList(devices, health);
  }
  if (health.voices && !$("voiceSelect").options.length) {
    fillSelect(
      $("voiceSelect"),
      health.voices.map((v) => ({ value: v, label: v })),
      health.voice,
      null,
    );
    $("voiceSelect").value = health.voice || "";
  }
  const vad = health.vad || {};
  if (document.activeElement !== $("silence") && vad.silence_ms) {
    $("silence").value = vad.silence_ms;
    $("silenceVal").textContent = String(vad.silence_ms);
  }
  if (document.activeElement !== $("energy") && vad.energy_threshold) {
    $("energy").value = vad.energy_threshold;
    $("energyVal").textContent = Number(vad.energy_threshold).toFixed(3);
  }
  if (document.activeElement !== $("maxUtter") && vad.max_utterance_seconds) {
    $("maxUtter").value = vad.max_utterance_seconds;
    $("maxVal").textContent = String(vad.max_utterance_seconds);
  }
  if (health.duck && health.duck.note) $("duckNote").textContent = health.duck.note;
}

function queueButtons(id) {
  const drop = document.createElement("button");
  drop.type = "button";
  drop.textContent = "Drop";
  drop.addEventListener("click", () => {
    post("/api/queue/drop", { id }).then((data) => {
      if (failed(data)) return;
      if (data.queue) renderQueue(data.queue);
    });
  });
  const answer = document.createElement("button");
  answer.type = "button";
  answer.textContent = "Answer";
  answer.addEventListener("click", () => {
    post("/api/queue/answer", { id }).then((data) => {
      if (failed(data)) return;
      if (data.queue) renderQueue(data.queue);
    });
  });
  return [drop, answer];
}

function renderQueue(queue) {
  if (!queueLog || !queue) return;
  queueLog.textContent = "";
  const alerts = queue.alerts || [];
  const chat = queue.chat || [];
  if (!alerts.length && !chat.length) {
    logLine(queueLog, "Queue is empty.");
    return;
  }
  for (const alert of alerts) {
    const row = document.createElement("div");
    row.className = "queue-row";
    const text = document.createElement("p");
    text.appendChild(el("span", "alert", alert.kind || "alert"));
    text.appendChild(document.createTextNode(" " + (alert.text || "")));
    row.appendChild(text);
    for (const button of queueButtons(alert.id)) row.appendChild(button);
    queueLog.appendChild(row);
  }
  for (const line of chat) {
    const row = document.createElement("div");
    row.className = "queue-row";
    const text = document.createElement("p");
    text.appendChild(el("span", "u", line.user || "?"));
    text.appendChild(document.createTextNode(" " + (line.text || "")));
    row.appendChild(text);
    for (const button of queueButtons(line.id)) row.appendChild(button);
    queueLog.appendChild(row);
  }
}

async function refreshHealth() {
  try {
    const res = await fetch("/api/health", { cache: "no-store" });
    if (!res.ok) return;
    const health = await res.json();
    renderSys(health);
    const st = health.status || {};
    applyStatus(st);
    const queueRes = await fetch("/api/queue", { cache: "no-store" });
    if (queueRes.ok) renderQueue(await queueRes.json());
    if (health.streamer && streamerName && streamerLogin && document.activeElement !== streamerName && document.activeElement !== streamerLogin) {
      if (!streamerName.value) streamerName.value = health.streamer.display_name || "";
      if (!streamerLogin.value) streamerLogin.value = health.streamer.twitch_login || "";
    }
  } catch (e) {
    console.warn("health poll failed", e);
  }
}

function applyStatus(st) {
  setPhase(st.phase);
  if (st.streamer_name) streamerLabel = st.streamer_name;
  if (st.last_user) lastUser.textContent = youLine(st.last_user);
  if (typeof st.caption === "string" && st.caption) caption.textContent = st.caption;
  listenOn = !!st.listen;
  muted = !!st.tts_mute;
  twitchState = st.twitch || twitchState;
  eventState = st.eventsub || eventState;
  listenBtn.textContent = listenOn ? "Stop listening" : "Start listening";
  listenBtn.classList.toggle("active", listenOn);
  muteBtn.textContent = muted ? "Unmute voice" : "Mute voice";
  muteBtn.classList.toggle("active", muted);
  twitchBtn.textContent = twitchState === "connected" ? "Disconnect chat" : "Connect chat";
  eventBtn.textContent = eventState === "connected" ? "Disconnect alerts" : "Connect alerts";
  if (st.skip && st.skip.reason) {
    $("skipLine").textContent = `Skipped: ${st.skip.reason}${st.skip.user ? " · " + st.skip.user : ""}`;
  }
  if (st.queue) renderQueue(st.queue);
  if (st.mood && document.activeElement !== $("mood")) $("mood").value = st.mood;
  const postVoice = $("postVoice");
  if (postVoice && document.activeElement !== postVoice && typeof st.post_voice === "boolean") {
    postVoice.checked = st.post_voice;
  }
  const duckBox = $("duck");
  if (duckBox && document.activeElement !== duckBox && typeof st.duck_enabled === "boolean") {
    duckBox.checked = st.duck_enabled;
  }
}

function selectedDeviceName(id) {
  const select = $(id);
  const option = select.options[select.selectedIndex];
  return option && option.dataset.name ? option.dataset.name : null;
}

function applyDeviceList(devices, health) {
  const ins = (devices.inputs || []).map((d) => ({
    value: d.index,
    name: d.name,
    label: `#${d.index} ${d.name}${d.cable ? " · virtual cable" : ""}`,
  }));
  const outs = (devices.outputs || []).map((d) => ({
    value: d.index,
    name: d.name,
    label: `#${d.index} ${d.name}${d.cable ? " · virtual cable" : ""}`,
  }));
  const audio = (health && health.audio) || {};
  fillSelect($("micSelect"), ins, audio.mic_device, "system default");
  fillSelect($("spkSelect"), outs, audio.speaker_device, "system default");
  devicesLoaded = true;
}

function saveDevices() {
  const mic = $("micSelect").value;
  const spk = $("spkSelect").value;
  post("/api/audio/devices", {
    mic_device: mic === "" ? null : Number(mic),
    speaker_device: spk === "" ? null : Number(spk),
    mic_device_name: mic === "" ? null : selectedDeviceName("micSelect"),
    speaker_device_name: spk === "" ? null : selectedDeviceName("spkSelect"),
  }).catch((e) => { hint.textContent = String(e.message || e); });
}
$("refreshDevices").addEventListener("click", async () => {
  devicesLoaded = false;
  try {
    const res = await fetch("/api/devices?refresh=1", { cache: "no-store" });
    if (!res.ok) throw new Error("could not refresh devices");
    const micNow = $("micSelect").value;
    const spkNow = $("spkSelect").value;
    applyDeviceList(await res.json(), {
      audio: {
        mic_device: micNow === "" ? null : Number(micNow),
        speaker_device: spkNow === "" ? null : Number(spkNow),
      },
    });
    hint.textContent = "Device list refreshed.";
  } catch (e) {
    hint.textContent = String(e.message || e);
  }
});
$("micSelect").addEventListener("change", saveDevices);
$("spkSelect").addEventListener("change", saveDevices);
$("langSelect").addEventListener("change", () => {
  post("/api/stt/language", { language: $("langSelect").value }).catch((e) => { hint.textContent = String(e.message || e); });
});
$("voiceSelect").addEventListener("change", () => {
  if (!$("voiceSelect").value) return;
  post("/api/tts/voice", { voice: $("voiceSelect").value }).then((data) => {
    if (failed(data)) return;
    hint.textContent = "Voice updated.";
  });
});

let vadTimer = 0;
function queueVad() {
  $("silenceVal").textContent = $("silence").value;
  $("energyVal").textContent = Number($("energy").value).toFixed(3);
  $("maxVal").textContent = $("maxUtter").value;
  clearTimeout(vadTimer);
  vadTimer = setTimeout(() => {
    post("/api/audio/vad", {
      silence_ms: Number($("silence").value),
      energy_threshold: Number($("energy").value),
      max_utterance_seconds: Number($("maxUtter").value),
    }).catch((e) => { hint.textContent = String(e.message || e); });
  }, 250);
}
$("silence").addEventListener("input", queueVad);
$("energy").addEventListener("input", queueVad);
$("maxUtter").addEventListener("input", queueVad);
$("duck").addEventListener("change", async () => {
  const data = await post("/api/audio/duck", { enabled: $("duck").checked });
  if (failed(data)) return;
  $("duckNote").textContent = data.note || "";
});

const profileSelect = $("profileSelect");
if (profileSelect) {
  profileSelect.addEventListener("change", () => {
    post("/api/profile", { name: profileSelect.value }).then((data) => {
      if (failed(data)) return;
      refreshHealth();
    });
  });
}
const replayBtn = $("replayBtn");
if (replayBtn) {
  replayBtn.addEventListener("click", () => {
    post("/api/queue/replay", {}).then((data) => {
      if (failed(data)) return;
      hint.textContent = "Replayed the last line.";
    });
  });
}
async function buildRecap(vibe) {
  const data = await post("/api/recap", { vibe: !!vibe });
  if (failed(data)) return data;
  const box = $("recapText");
  if (box) {
    box.value = data.markdown || "";
    box.dataset.copied = "";
  }
  return data;
}
const recapBtn = $("recapBtn");
if (recapBtn) {
  recapBtn.addEventListener("click", () => {
    buildRecap(false).catch((e) => { hint.textContent = String(e.message || e); });
  });
}
const recapVibeBtn = $("recapVibeBtn");
if (recapVibeBtn) {
  recapVibeBtn.addEventListener("click", () => {
    buildRecap(true).catch((e) => { hint.textContent = String(e.message || e); });
  });
}
const recapCopy = $("recapCopy");
if (recapCopy) {
  recapCopy.addEventListener("click", async () => {
    const box = $("recapText");
    const text = box ? box.value : "";
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      if (box) {
        box.focus();
        box.select();
      }
    }
    if (box) box.dataset.copied = "1";
    hint.textContent = "Recap copied.";
  });
}

async function loadPersona() {
  try {
    const res = await fetch("/api/persona", { cache: "no-store" });
    const data = await res.json();
    if (document.activeElement !== $("personaText")) $("personaText").value = data.text || "";
  } catch {
    /* persona editor can stay empty */
  }
}

function showReconnect(on) {
  const banner = $("reconnectBanner");
  if (!banner) return;
  banner.hidden = !on;
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => showReconnect(false);
  ws.onerror = () => showReconnect(true);
  ws.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (msg.type === "status") {
      applyStatus(msg.data || {});
    } else if (msg.type === "level") {
      const data = msg.data;
      const level = Math.min(1, data && typeof data === "object" ? Number(data.level) || 0 : Number(data) || 0);
      const wake = data && typeof data === "object" ? Number(data.wake) || 0 : 0;
      if (meter) meter.style.width = `${Math.round(level * 400)}%`;
      const wakeEl = $("wakeScore");
      if (wakeEl) wakeEl.textContent = wake > 0 ? `wake ${wake.toFixed(2)}` : "";
    } else if (msg.type === "chat") {
      const d = msg.data || {};
      const label = d.streamer ? `${d.user || ""} (streamer)` : (d.user || personaName);
      logParts(chatLog, [{ cls: d.bot ? "bot" : "u", text: label }, { text: " " + (d.text || "") }]);
    } else if (msg.type === "alert") {
      const d = msg.data || {};
      const line = d.text || `${d.kind || "alert"} ${d.user || ""}`;
      logParts(alertLog, [{ cls: "alert", text: d.kind || "alert" }, { text: " " + line }]);
      logParts(eventLog, [{ cls: "alert", text: d.kind || "alert" }, { text: " " + line }]);
    } else if (msg.type === "transcript") {
      const d = msg.data || {};
      logParts(eventLog, [{ cls: "u", text: d.role || "" }, { text: " " + (d.text || "") }]);
      if (d.role === "cohost") caption.textContent = d.text || "";
      if (d.role === "streamer") lastUser.textContent = youLine(d.text || "");
    } else if (msg.type === "memory") {
      renderMemory(msg.data);
    } else if (msg.type === "skip") {
      const d = msg.data || {};
      $("skipLine").textContent = `Skipped: ${d.reason || ""}${d.user ? " · " + d.user : ""}`;
    } else if (msg.type === "log") {
      logLine(eventLog, String(msg.data || ""), "warn");
    }
  };
  ws.onclose = () => {
    showReconnect(true);
    setTimeout(connectWs, 1500);
  };
}

refreshHealth();
setInterval(refreshHealth, 8000);
loadPersona();
connectWs();
