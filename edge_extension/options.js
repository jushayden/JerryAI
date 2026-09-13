"use strict";
const form = document.getElementById("pair-form"), input = document.getElementById("token"), status = document.getElementById("status"), button = form.querySelector("button");
chrome.storage.local.get("localToken").then(({ localToken }) => { if (localToken) input.value = localToken; });
form.addEventListener("submit", async event => {
  event.preventDefault(); button.disabled = true;
  try {
    const localToken = input.value.trim();
    if (localToken.length < 32 || localToken.length > 256) throw new Error("Use the complete key from .local-token.");
    await chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
    await chrome.storage.local.set({ localToken });
    status.textContent = "Saved. Checking your local agent…";
    const result = await chrome.runtime.sendMessage({ type: "tora:health" });
    if (!result || result.error) throw new Error(result?.error || "No response from Tora. Start the agent and retry.");
    status.textContent = result.telegram_paired ? "Connected. Open a website and use the T badge to begin." : "Connected. Send /start to your bot, set ALLOWED_CHAT_ID in .env, and restart Tora.";
  } catch (error) { status.textContent = `Pairing check: ${error.message}`; }
  finally { button.disabled = false; }
});
