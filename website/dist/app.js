(() => {
  "use strict";
  const config = window.TORA_SITE || {};
  const menu = document.querySelector(".menu-toggle");
  const nav = document.getElementById("navigation");
  function closeMenu(returnFocus = false) {
    nav.classList.remove("is-open"); menu.setAttribute("aria-expanded", "false");
    menu.setAttribute("aria-label", "Open navigation");
    if (returnFocus) menu.focus();
  }
  menu.addEventListener("click", () => {
    const opened = nav.classList.toggle("is-open");
    menu.setAttribute("aria-expanded", String(opened));
    menu.setAttribute("aria-label", opened ? "Close navigation" : "Open navigation");
  });
  nav.addEventListener("click", e => { if (e.target.closest("a")) closeMenu(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && nav.classList.contains("is-open")) closeMenu(true); });
  document.addEventListener("click", e => { if (!e.target.closest(".nav-wrap")) closeMenu(); });
  window.matchMedia("(min-width: 651px)").addEventListener("change", closeMenu);
  try {
    const repository = new URL(config.repository);
    if (repository.protocol === "https:") document.querySelectorAll("[data-repo]").forEach(a => { a.href = repository.href; });
  } catch { /* Checked-in repository links remain available. */ }
  const toast = document.getElementById("toast");
  let toastTimer;
  function announce(message) {
    clearTimeout(toastTimer); toast.textContent = message; toast.classList.add("visible");
    toastTimer = setTimeout(() => toast.classList.remove("visible"), 3200);
  }
  document.querySelectorAll("[data-copy]").forEach(button => button.addEventListener("click", async () => {
    const node = document.getElementById(button.dataset.copy);
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(node.textContent); announce("Command copied.");
    } catch {
      const selection = window.getSelection(), range = document.createRange();
      range.selectNodeContents(node); selection.removeAllRanges(); selection.addRange(range);
      announce("Command selected. Press Ctrl+C or use your device's Copy action.");
    }
  }));
  const scenarios = {
    research: {
      prompt: "“Research a topic and save a short brief with source links.”",
      description: "Follow the steps from a Telegram request to a sourced research brief.",
      steps: [["Receive the goal", "A task is added to the local queue."], ["Read and collect", "Browser tools gather relevant page content."], ["Write the brief", "The local model works with the observed information."], ["Return the evidence", "A summary and source links go back to Telegram."]],
    },
    files: {
      prompt: "“Create a project folder and write a short getting-started note.”",
      description: "See how a file task moves from a message to a verified local result.",
      steps: [["Receive the task", "The local worker picks up the request."], ["Check the location", "File tools resolve paths inside the configured folder."], ["Create the files", "The agent writes the requested note and records the path."], ["Verify on disk", "The report includes the file's existence and size."]],
    },
    inbox: {
      prompt: "“Give me a briefing on my recent inbox messages.”",
      description: "Explore a read-only Gmail briefing. The local agent needs your own Gmail OAuth setup.",
      steps: [["Queue the briefing", "The /inbox command creates a task."], ["Read recent messages", "The Gmail API returns message metadata and snippets."], ["Prepare the summary", "The local model looks for deadlines and follow-ups."], ["Send the briefing", "A concise report goes to your Telegram chat."]],
    },
  };
  const tabs = [...document.querySelectorAll("[data-scenario]")];
  const panel = document.getElementById("walkthrough-panel");
  const steps = document.getElementById("trace-steps");
  const play = document.getElementById("walkthrough-play");
  const status = document.getElementById("trace-status");
  let timer = null, index = -1, playing = false;
  function stop() { clearTimeout(timer); timer = null; playing = false; }
  function selectScenario(tab) {
    stop(); index = -1;
    tabs.forEach(t => { const active = t === tab; t.setAttribute("aria-selected", String(active)); t.tabIndex = active ? 0 : -1; });
    panel.setAttribute("aria-labelledby", tab.id);
    const scenario = scenarios[tab.dataset.scenario];
    document.getElementById("sample-prompt").textContent = scenario.prompt;
    document.getElementById("sample-description").textContent = scenario.description;
    steps.replaceChildren(...scenario.steps.map(([title, description], i) => {
      const li = document.createElement("li"), marker = document.createElement("span"), body = document.createElement("div"), strong = document.createElement("strong"), p = document.createElement("p");
      marker.className = "trace-step-icon"; marker.textContent = String(i + 1);
      strong.textContent = title; p.textContent = description; body.append(strong, p); li.append(marker, body);
      return li;
    }));
    status.textContent = "Ready to explore"; play.textContent = "▷ Play walkthrough";
  }
  function renderTrace() {
    [...steps.children].forEach((li, i) => {
      li.classList.toggle("trace-done", i < index || index >= 4);
      li.classList.toggle("trace-active", i === index);
      li.querySelector("span").textContent = i < index || index >= 4 ? "✓" : String(i + 1);
    });
    if (index >= 4) {
      stop(); status.textContent = "Example complete"; play.textContent = "↻ Replay walkthrough"; return;
    }
    status.textContent = `Example step ${index + 1} of 4`;
    play.textContent = "Ⅱ Pause walkthrough";
    timer = setTimeout(() => { index++; renderTrace(); }, 1500);
  }
  play.addEventListener("click", () => {
    if (playing) { stop(); play.textContent = "▷ Continue walkthrough"; status.textContent = "Walkthrough paused"; return; }
    playing = true;
    if (index < 0 || index >= 4) index = 0;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) index = 4;
    renderTrace();
  });
  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => selectScenario(tab));
    tab.addEventListener("keydown", e => {
      let target = i;
      if (e.key === "ArrowRight") target = (i + 1) % tabs.length;
      else if (e.key === "ArrowLeft") target = (i + tabs.length - 1) % tabs.length;
      else if (e.key === "Home") target = 0;
      else if (e.key === "End") target = tabs.length - 1;
      else return;
      e.preventDefault(); tabs[target].focus(); selectScenario(tabs[target]);
    });
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && playing) { stop(); play.textContent = "▷ Continue walkthrough"; status.textContent = "Walkthrough paused"; }
  });
  // Optional RECORDED demo only. Live agent integration is intentionally left for
  // the owner's next specification; never contact the owner's loopback service.
  if (config.demoVideoUrl) {
    try {
      const url = new URL(config.demoVideoUrl, location.href);
      if (url.protocol !== "https:" && !(url.origin === location.origin && url.protocol === "http:")) throw new Error("Unsupported video URL");
      const slot = document.getElementById("live-demo-slot");
      slot.replaceChildren(); slot.classList.add("has-video");
      const label = document.createElement("span"), title = document.createElement("strong"), video = document.createElement("video");
      label.className = "live-label"; label.textContent = "RECORDED DEMO";
      title.textContent = config.demoVideoLabel || "Tora AI walkthrough";
      video.controls = true; video.preload = "metadata"; video.playsInline = true;
      video.setAttribute("aria-label", title.textContent); video.src = url.href;
      const error = document.createElement("p"); error.hidden = true;
      video.addEventListener("error", () => { error.hidden = false; error.textContent = "This recording is unavailable. Explore the walkthrough above or try Tora locally."; });
      slot.append(label, title, video, error);
    } catch { /* Keep the explicit unconnected state. */ }
  }
  const sectionObserver = new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) {
      nav.querySelectorAll("a").forEach(a => {
        const current = a.hash === `#${entry.target.id}`;
        a.classList.toggle("active", current);
        if (current) a.setAttribute("aria-current", "location"); else a.removeAttribute("aria-current");
      });
    }
  }, { rootMargin: "-20% 0px -55% 0px" });
  document.querySelectorAll("main section[id]").forEach(section => sectionObserver.observe(section));
})();
