(() => {
  "use strict";
  if (window.top !== window || document.getElementById("tora-ai-extension")) return;
  const host = document.createElement("div"); host.id = "tora-ai-extension";
  const shadow = host.attachShadow({ mode: "closed" });
  const css = document.createElement("link"); css.rel = "stylesheet"; css.href = chrome.runtime.getURL("content.css");
  const badge = document.createElement("button"); badge.className = "badge"; badge.textContent = "T";
  badge.type = "button"; badge.title = "Tora AI"; badge.setAttribute("aria-label", "Open Tora AI"); badge.setAttribute("aria-expanded", "false");
  const panel = document.createElement("section"); panel.className = "panel"; panel.hidden = true;
  panel.innerHTML = '<header><strong>Tora AI</strong><button type="button" class="settings">Pair / Options</button></header><label for="tora-prompt">What should I do on this page?</label><textarea id="tora-prompt" maxlength="4000" placeholder="Describe your task…"></textarea><button type="button" class="send">Send to Tora</button><p class="status" role="status" aria-live="polite"></p><small>Approvals and full reports arrive in Telegram.</small>';
  shadow.append(css, panel, badge); document.documentElement.appendChild(host);
  const input = panel.querySelector("textarea"), send = panel.querySelector(".send"), status = panel.querySelector(".status");
  let timer = null, busy = false, failures = 0;
  function finish() { busy = false; send.disabled = false; badge.classList.remove("busy"); clearTimeout(timer); }
  async function api(message) {
    const result = await chrome.runtime.sendMessage(message);
    if (!result) throw new Error("Reload this page after updating the extension.");
    return result;
  }
  async function poll(id) {
    try {
      const task = await api({ type: "tora:status", id });
      if (task.error) {
        if ([403, 404].includes(task.status)) { status.textContent = task.error; finish(); return; }
        throw new Error(task.error);
      }
      failures = 0;
      if (task.status === "queued") status.textContent = "Queued. Follow progress in Telegram.";
      else if (task.status === "running") status.textContent = task.needs ? `Needs you: ${task.needs}` : `Working — step ${task.steps}: ${task.step || "starting"}`;
      else if (["done", "failed", "needs_attention", "cancelled"].includes(task.status)) {
        status.textContent = `${task.status}: ${task.result || task.needs || "Check Telegram for details."}`; finish(); return;
      } else throw new Error("Unexpected task status. Check Telegram.");
    } catch (error) {
      failures++;
      status.textContent = failures >= 5 ? `Connection lost. ${error.message} Check Telegram before sending the task again.` : "Reconnecting to Tora…";
      if (failures >= 5) { finish(); return; }
    }
    timer = setTimeout(() => poll(id), Math.min(1500 * (failures + 1), 6000));
  }
  async function submit() {
    const text = input.value.trim(); if (!text || busy) return;
    busy = true; failures = 0; send.disabled = true; badge.classList.add("busy"); status.textContent = "Sending…";
    try {
      const result = await api({ type: "tora:task", text });
      if (result.error) throw new Error(result.error);
      if (!/^[a-f0-9]{6}$/.test(result.id || "")) throw new Error("Unexpected task response.");
      input.value = ""; status.textContent = `Queued as ${result.id}.`; timer = setTimeout(() => poll(result.id), 1000);
    } catch (error) { finish(); status.textContent = `${error.message} Check that python main.py is running.`; }
  }
  // Only a real user gesture can enqueue work; scripts on a visited page cannot.
  send.addEventListener("click", event => { if (event.isTrusted) submit(); });
  input.addEventListener("keydown", event => {
    if (event.isTrusted && event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submit(); }
  });
  badge.addEventListener("click", event => {
    if (!event.isTrusted) return;
    panel.hidden = !panel.hidden; badge.setAttribute("aria-expanded", String(!panel.hidden));
    if (!panel.hidden) input.focus();
  });
  panel.addEventListener("keydown", event => { if (event.key === "Escape") { panel.hidden = true; badge.setAttribute("aria-expanded", "false"); badge.focus(); } });
  panel.querySelector(".settings").addEventListener("click", event => {
    if (event.isTrusted) api({ type: "tora:options" }).catch(() => { status.textContent = "Open Options from edge://extensions."; });
  });
  window.addEventListener("pagehide", () => clearTimeout(timer));
  window.addEventListener("pageshow", event => { if (event.persisted && busy) { finish(); status.textContent = "Page restored. Check Telegram for your task's result."; } });
})();
