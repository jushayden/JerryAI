// Jerry Pocket Agent J badge: prompt the local agent and attach user-selected files.
(() => {
  if (window.top !== window) return;
  const BASE = "http://127.0.0.1:8765";
  const TOKEN = "pocket-agent-local";
  const TAB_TOKEN = crypto.randomUUID();
  document.documentElement.dataset.jerryTabToken = TAB_TOKEN;

  const badge = document.createElement("div");
  badge.id = "pocket-agent-badge";
  badge.textContent = "J";
  badge.title = "Jerry Pocket Agent";

  const panel = document.createElement("div");
  panel.id = "pocket-agent-panel";
  panel.innerHTML = `
    <h1>Jerry Pocket Agent</h1>
    <textarea id="pocket-agent-input"
      placeholder="What should I do on this page?"></textarea>
    <input id="pocket-agent-files" type="file" multiple hidden>
    <div id="pocket-agent-attachments"></div>
    <div class="pa-actions">
      <button id="pocket-agent-add" type="button" title="Attach files" aria-label="Attach files">+</button>
      <span class="pa-attach-label">Attach files</span>
      <button id="pocket-agent-stop" type="button" hidden>Stop</button>
      <button id="pocket-agent-send">Do it</button>
    </div>
    <div id="pocket-agent-status"></div>`;

  document.documentElement.appendChild(badge);
  document.documentElement.appendChild(panel);

  const input = panel.querySelector("#pocket-agent-input");
  const send = panel.querySelector("#pocket-agent-send");
  const add = panel.querySelector("#pocket-agent-add");
  const stop = panel.querySelector("#pocket-agent-stop");
  const fileInput = panel.querySelector("#pocket-agent-files");
  const attachments = panel.querySelector("#pocket-agent-attachments");
  const status = panel.querySelector("#pocket-agent-status");
  let polling = null;
  let selectedFiles = [];
  let activeTaskId = null;
  let activeController = null;
  let stopping = false;

  badge.addEventListener("click", () => {
    panel.classList.toggle("pa-open");
    if (panel.classList.contains("pa-open")) input.focus();
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      send.click();
    }
  });

  async function api(path, opts = {}) {
    const isForm = opts.body instanceof FormData;
    const resp = await fetch(BASE + path, {
      ...opts,
      headers: {
        "X-Pocket-Token": TOKEN,
        ...(isForm ? {} : { "Content-Type": "application/json" }),
        ...(opts.headers || {}),
      },
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
  }

  const esc = (value) => String(value ?? "").replace(/[&<>"]/g,
    (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[char]));

  function renderWorking(ack, steps) {
    const items = (steps || []).map((step) => `<li>${esc(step)}</li>`).join("");
    status.innerHTML = `
      <div class="pa-live">
        <div class="pa-spinner"></div>
        <div class="pa-ack">${esc(ack)}</div>
      </div>
      <ul class="pa-steps">${items}</ul>`;
  }

  function renderDone(task) {
    const icon = task.status === "done" ? "✅" : task.status === "needs_attention" ? "💬" : "⚠️";
    const detail = task.result || task.needs || task.status;
    status.innerHTML = `
      <div class="pa-verdict">${icon} ${task.status === "done" ? "Done" : esc(task.status)}</div>
      <div class="pa-result">${esc(detail)}</div>`;
  }

  function renderAttachments() {
    attachments.innerHTML = selectedFiles.map((file, index) => `
      <div class="pa-file-chip">
        <span title="${esc(file.name)}">${esc(file.name)}</span>
        <button type="button" data-remove-file="${index}" aria-label="Remove ${esc(file.name)}">×</button>
      </div>`).join("");
  }

  add.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    for (const file of Array.from(fileInput.files || [])) {
      const duplicate = selectedFiles.some((existing) =>
        existing.name === file.name && existing.size === file.size &&
        existing.lastModified === file.lastModified);
      if (!duplicate) selectedFiles.push(file);
    }
    fileInput.value = "";
    renderAttachments();
  });

  attachments.addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-file]");
    if (!button) return;
    selectedFiles.splice(Number(button.dataset.removeFile), 1);
    renderAttachments();
  });

  async function uploadAttachments(signal) {
    const uploaded = [];
    for (let index = 0; index < selectedFiles.length; index += 1) {
      const file = selectedFiles[index];
      renderWorking(`Attaching ${index + 1} of ${selectedFiles.length}: ${file.name}`, []);
      const body = new FormData();
      body.append("file", file, file.name);
      uploaded.push(await api("/upload", { method: "POST", body, signal }));
    }
    return uploaded;
  }

  function resetControls() {
    badge.classList.remove("pa-busy");
    send.disabled = false;
    add.disabled = false;
    stop.hidden = true;
    activeTaskId = null;
    activeController = null;
  }

  stop.addEventListener("click", async () => {
    stopping = true;
    stop.disabled = true;
    activeController?.abort();
    clearInterval(polling);
    try {
      if (activeTaskId) {
        await api(`/task/${activeTaskId}/cancel`, { method: "POST", body: "{}" });
      }
    } catch { /* It may already have finished or the upload was still local. */ }
    resetControls();
    stop.disabled = false;
    status.innerHTML = '<div class="pa-verdict">Stopped</div><div class="pa-result">Jerry will not continue this task.</div>';
  });

  send.addEventListener("click", async () => {
    const text = input.value.trim() || (selectedFiles.length
      ? "Upload the attached file to the appropriate file field on this page. Do not submit."
      : "");
    if (!text) return;
    send.disabled = true;
    add.disabled = true;
    stop.hidden = false;
    stopping = false;
    activeController = new AbortController();
    badge.classList.add("pa-busy");
    renderWorking("Ok — let me start on that…", []);
    try {
      const uploaded = await uploadAttachments(activeController.signal);
      const { id } = await api("/task", {
        method: "POST",
        signal: activeController.signal,
        body: JSON.stringify({
          text,
          url: location.href,
          tab_token: TAB_TOKEN,
          attachments: uploaded.map((file) => file.path),
        }),
      });
      activeTaskId = id;
      input.value = "";
      selectedFiles = [];
      renderAttachments();
      clearInterval(polling);
      polling = setInterval(async () => {
        try {
          const task = await api(`/task/${id}`);
          if (task.status === "running") {
            renderWorking("On it — here's what I'm doing:", task.recent_steps);
          } else if (task.status === "queued") {
            renderWorking("Queued — starting in a moment…", []);
          } else {
            clearInterval(polling);
            resetControls();
            renderDone(task);
          }
        } catch { /* Jerry may be restarting; keep polling. */ }
      }, 1200);
    } catch (error) {
      resetControls();
      if (stopping || error.name === "AbortError") return;
      status.textContent =
        "❌ Can't reach Jerry Pocket Agent — is `python main.py` running? (" + error.message + ")";
    }
  });
})();
