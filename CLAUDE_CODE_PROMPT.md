# Claude Code handoff — Tora AI website and local agent

You are working in the `jushayden/Tora-AI` repository. Read the existing code and docs before making changes. The product is a local-first Windows AI agent: Ollama handles local reasoning, Playwright drives a visible Edge session, Python tools handle files and apps, Gmail is read-only and optional, and Telegram is the control channel.

A polished static product website is already implemented in `website/dist/`:

- `index.html` is the responsive presentation page.
- `styles.css` is the dark graphite / amber visual system.
- `core.js` renders the interactive procedural WebGL agent core with a reduced-motion fallback and a pause button.
- `app.js` handles mobile navigation, active section state, keyboard-accessible workflow tabs, scripted walkthroughs, copy buttons, and an optional recorded video setting.
- `site-config.js` contains public-only settings. `demoVideoUrl` is intentionally empty.
- `downloads/` contains the source ZIP, Edge extension ZIP, and quick-start guide.

The website must stay separate from the agent runtime. Do not make the public page call `127.0.0.1`, read browser storage, accept bot tokens, or ask visitors to expose a local port. When the owner later supplies a live-demo design, implement it behind an explicit safe server boundary and keep credentials out of browser code. A recorded HTTPS or same-origin video can be enabled by setting `demoVideoUrl` in `site-config.js`.

The local-agent improvements already in this branch are intentional:

- `scripts/setup.py` creates `.venv`, installs requirements, installs Playwright Chromium, copies `.env.example`, and creates a per-install `.local-token`.
- The Edge extension uses a trusted MV3 service worker plus an Options page. The token is stored in extension storage, never in page JavaScript. `content.js` only sends tasks after a real user gesture.
- `server.py` is loopback-only, validates origin, token, prompt length, URL scheme, queue size, and pairing state. It exposes `/health`, `/task`, and `/task/{id}`.
- `ALLOWED_CHAT_ID=0` no longer lets the first person who sends `/start` claim the bot. `/start` displays the chat ID; the owner must set it in `.env` and restart.
- Missing `profile.yaml` is a hard error, overwrite and destructive file operations require confirmation, remembered facts are written as valid YAML, model readiness requires the requested tag, and startup/shutdown cleanup is guarded.
- CAPTCHA and human-verification boundaries, approval gates, secret handles, and source-tab selection must remain intact.

Your work:

1. Inspect the current `git diff`, then make only changes that improve the requested Tora AI experience or fix a concrete defect. Preserve the current dependency choices and static site architecture.
2. If adding a live demo, first write down the exact request/response boundary and threat model. Keep it disabled until it has an explicit configuration flag, input validation, rate limiting, and a safe failure state. Never proxy arbitrary localhost addresses or place credentials in `website/dist`.
3. Keep all visible copy accurate: Tora AI is local-first, the Gmail integration is read-only and optional, consequential actions require Telegram approval, and the agent stops at CAPTCHA/bot checks.
4. Keep downloads reproducible. If source files change, rebuild `release/tora-ai-source.zip` and copy it to `website/dist/downloads/`. Do not package `.env`, `.local-token`, `profile.yaml`, credentials, tokens, browser profiles, logs, screenshots, artifacts, or `.venv`.
5. Run these checks before finishing:

   ```powershell
   node --check website/dist/core.js
   node --check website/dist/app.js
   node --check edge_extension/background.js
   node --check edge_extension/content.js
   python -m py_compile agent.py bridge.py config.py gate.py main.py server.py state.py tools_browser.py tools_email.py tools_fs.py scripts/setup.py
   python test_state.py
   python test_tools_fs.py
   python test_remote_operator.py
   python test_vision.py
   ```

   On Windows, also run `python test_browser.py` and one manual Edge co-drive smoke test. The browser test needs a visible desktop and the Ollama model is not required for the deterministic tests.

6. Finish with a concise summary of changed files, test results, and any live-demo work that remains intentionally unconnected.
