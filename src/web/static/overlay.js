const phaseEl = document.getElementById("phase");
const lineEl = document.getElementById("line");
const nameEl = document.getElementById("name");

async function boot() {
  try {
    const health = await fetch("/api/health").then((r) => r.json());
    if (health.persona) nameEl.textContent = health.persona;
  } catch {
    /* overlay still works via ws */
  }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "status") {
      const st = msg.data || {};
      phaseEl.textContent = st.phase || "idle";
      phaseEl.className = "pill " + (st.phase || "idle");
      if (st.persona) nameEl.textContent = st.persona;
      if (st.caption) lineEl.textContent = st.caption;
    } else if (msg.type === "transcript" && msg.data && msg.data.role === "cohost") {
      lineEl.textContent = msg.data.text;
    }
  };
  ws.onclose = () => setTimeout(connect, 1500);
}

boot();
connect();
