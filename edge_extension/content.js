// Pocket Agent J badge — sends prompts (+ current URL) to the local agent.
(() => {
  if (window.top !== window) return; // main frame only, not every iframe
  const BASE = "http://127.0.0.1:8765";
  const TOKEN = "pocket-agent-local";

  const badge = document.createElement("div");
  badge.id = "pocket-agent-badge";
  badge.textContent = "J";
  badge.title = "Pocket Agent";

  const panel = document.createElement("div");
  panel.id = "pocket-agent-panel";
  panel.innerHTML = `
    <h1>Pocket Agent</h1>
    <textarea id="pocket-agent-input"
      placeholder="What should I do? (this page's URL is sent along)"></textarea>
    <button id="pocket-agent-send">Do it</button>
    <div id="pocket-agent-status"></div>`;

  document.documentElement.appendChild(badge);
  document.documentElement.appendChild(panel);

  const input = panel.querySelector("#pocket-agent-input");
  const send = panel.querySelector("#pocket-agent-send");
  const status = panel.querySelector("#pocket-agent-status");
  let polling = null;

  badge.addEventListener("click", () => {
    panel.classList.toggle("pa-open");
    if (panel.classList.contains("pa-open")) input.focus();
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send.click();
    }
  });

  async function api(path, opts = {}) {
    const resp = await fetch(BASE + path, {
      ...opts,
      headers: {
        "Content-Type": "application/json",
        "X-Pocket-Token": TOKEN,
        ...(opts.headers || {}),
      },
    });
    return resp.json();
  }

  send.addEventListener("click", async () => {
    const text = input.value.trim();
    if (!text) return;
    send.disabled = true;
    badge.classList.add("pa-busy");
    status.textContent = "Sending…";
    try {
      const { id, error } = await api("/task", {
        method: "POST",
        body: JSON.stringify({ text, url: location.href }),
      });
      if (error) throw new Error(error);
      status.textContent = `Queued as ${id}…`;
      input.value = "";
      clearInterval(polling);
      polling = setInterval(async () => {
        try {
          const t = await api(`/task/${id}`);
          if (t.status === "running") {
            status.textContent = `Working — step ${t.steps}: ${t.step || "starting"}`;
          } else if (t.status === "queued") {
            status.textContent = "Queued…";
          } else {
            clearInterval(polling);
            badge.classList.remove("pa-busy");
            send.disabled = false;
            const tag = t.status === "done" ? "✅" : "⚠️ " + t.status;
            status.textContent = `${tag} ${t.result || t.needs || ""}`;
          }
        } catch { /* agent restarting; keep polling */ }
      }, 1500);
    } catch (e) {
      badge.classList.remove("pa-busy");
      send.disabled = false;
      status.textContent =
        "❌ Can't reach Pocket Agent — is `python main.py` running? (" + e.message + ")";
    }
  });
})();
