/* Pairing credentials live only in trusted extension storage, never in a page. */
"use strict";
const BASE = "http://127.0.0.1:8765";
chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" }).catch(() => {});
chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id || !message || typeof message !== "object") return false;
  const optionsPage = sender.url === chrome.runtime.getURL("options.html");
  const page = sender.tab && sender.frameId === 0 && /^https?:\/\//.test(sender.url || "");
  if (!optionsPage && !page) return false;
  if (message.type === "tora:options" && page) {
    chrome.runtime.openOptionsPage(); sendResponse({ ok: true }); return false;
  }
  let path, init = {};
  if (message.type === "tora:health" && optionsPage) path = "/health";
  else if (message.type === "tora:task" && page) {
    if (typeof message.text !== "string" || !message.text.trim() || message.text.length > 4000) {
      sendResponse({ error: "Use a prompt between 1 and 4000 characters." }); return false;
    }
    path = "/task";
    init = { method: "POST", body: JSON.stringify({ text: message.text.trim(), url: sender.url }) };
  } else if (message.type === "tora:status" && page && /^[a-f0-9]{6}$/.test(message.id || "")) {
    path = `/task/${message.id}`;
  } else return false;
  (async () => {
    try {
      const { localToken } = await chrome.storage.local.get("localToken");
      if (!localToken || localToken.length < 32) throw new Error("Pair Tora first using the extension Options page.");
      const response = await fetch(BASE + path, {
        ...init, credentials: "omit", redirect: "error", signal: AbortSignal.timeout(10000),
        headers: { "Content-Type": "application/json", "X-Pocket-Token": localToken },
      });
      let data;
      try { data = await response.json(); } catch { throw new Error(`Local service returned HTTP ${response.status}.`); }
      if (!response.ok) sendResponse({ error: data.error || `HTTP ${response.status}`, status: response.status });
      else sendResponse(data);
    } catch (error) {
      sendResponse({ error: error.name === "TimeoutError" ? "Tora did not respond. Check that it is running." : error.message });
    }
  })();
  return true;
});
