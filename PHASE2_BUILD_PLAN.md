# Pocket Agent — Phase 2 Build Plan (self-contained; written for a fresh Claude/Opus session)

Read this top to bottom before writing code. Every claim about current state below is
grounded in the code in this repo and in live testing done on 2026-07-03/10 — not in
intentions. Where something is unverified, it says so.

## 1. What this project is

Local-first agentic AI ("Pocket Agent") on the user's Windows 11 PC (RTX 5080 16GB,
31GB RAM, Python 3.11). Reasoning runs entirely on a local model — **no OpenAI/Anthropic
API anywhere in the loop**. The user controls it from their phone via a Telegram bot
(@JerryAAHK_bot) and from a floating "J" badge extension in Edge. Hackathon demo project;
the code is real and kept lean: plain module-level async functions, one asyncio process,
no classes beyond dataclasses, no frameworks beyond what's listed.

Run: `python main.py` (starts everything: mock-form http server on :8000, Ollama check +
model warm-up, Telegram polling, J-badge endpoint on 127.0.0.1:8765).
Stack: Ollama `qwen3-coder:30b` (pinned, `keep_alive:-1`, ~2–4s per warm task),
python-telegram-bot 22.8, Playwright async (headed Chromium), aiohttp (J-badge endpoint).

## 2. Verified current state

### Working end-to-end (live-verified on the real phone + real model)
- Phone→PC task loop: Telegram message → serial asyncio queue → agent loop → tools →
  live-edited status card → final report + proof screenshot to phone. `/brief`, `/status`,
  `/cancel`, `/testconfirm`, `!` prefix = preauthorized task.
- Local tool-calling loop (`agent.py run_task`): 15-step cap, 120s per model call
  (`config.MODEL_CALL_TIMEOUT`), tool errors returned as strings (model self-corrects),
  repeated-identical-failing-call breaker, results truncated to `config.TOOL_RESULT_MAX`.
  Cross-task context: `main.py` injects summaries of the last 5 terminal tasks INTO THE
  SINGLE system message (a second system message gets dropped by qwen3-coder's chat
  template — this failed live; do not regress it).
- FS/PC tools (`tools_fs.py`), all sandboxed via `_safe()` (expandvars + expanduser +
  resolve + `is_relative_to(config.SANDBOX_ROOT)`): read/write/list/move, gated
  delete (folders via rmtree; approval card shows item count), `open_app` (fixed dict
  allowlist), `read_profile`, `take_screenshot` (mss). `touched` list records
  written/moved/deleted paths; `main._verify_touched()` appends "Verified on disk:" lines
  to reports (deterministic proof — a screenshot of the screen proves nothing about disk).
- Approval gate (`gate.py` + `bridge.confirm`): deterministic classifier
  (`is_irreversible_click`: type=submit, in-form button, or SUBMIT_WORDS regex),
  inline Approve/Deny keyboard on the phone, timeout=deny, denial string tells the model
  not to retry. Verified live multiple times including a real folder deletion.
- J badge (`edge_extension/` + `server.py`): content script injects a floating J on every
  page, prompt box POSTs {text, url} to 127.0.0.1:8765 with header `X-Pocket-Token`
  (must equal `config.LOCAL_TOKEN`), polls GET /task/{id} for live status. Verified live.
- Browser form tools (`tools_browser.py`) on cooperative pages (25/25 checks on the local
  mock form): frame-walking extractor tags actionable elements with `data-agent-id`
  (`e{n}` main frame, `f{i}e{n}` iframes), compact labeled digest, fill/select/click/
  upload via those ids only (model never writes selectors), gate wired inside
  `click_element`, every action re-extracts and appends a fresh digest.

### Partially built / built but unproven
- `tools_email.py` + `GMAIL_SETUP.md` EXIST and are offline-tested (import, schema
  contract, graceful no-credentials error). One tool: `scan_inbox(hours=24)` — Gmail
  read-only API digest. NOT yet wired into the agent's registry, and the live OAuth flow
  is untested until the user creates `credentials.json` (guided in GMAIL_SETUP.md).
  `.gitignore` already covers credentials.json + token.json.
- `upload_file` (in tools_browser.py): written, sandbox-checked, never exercised against
  a real file input.
- Web search: system prompt teaches DuckDuckGo html + read_page; not yet stress-tested.
- `profile.yaml`: currently the fake "Alex Demo" persona with placeholder resume_path.

### Live-observed bugs Phase 2 must fix (all reproduced on 2026-07-03)
1. **JS-heavy pages extract too early.** `browser_goto` waits for "load" only; on the
   Tesla configurator the digest came back before the app rendered → model wandered.
2. **Blocked ≠ report-blockage.** When Tesla's bot protection denied access, the model
   fabricated a local "sample form" file and reported the task as demonstrated. Must be
   clamped in the system prompt: blocked → say so, never produce substitute artifacts.
3. **Agent-owned Chromium is second-class on protected sites** (no user session/cookies;
   Akamai-class walls reject it). This motivates co-drive mode (P0).

## 3. Contracts a new session must respect (do not redesign)

- **Tool registry**: every tools_*.py exposes
  `TOOLS: dict[name, {"schema": <Ollama {"type":"function","function":{...}}>, "fn": async (args: dict) -> str}]`.
  Tool fns NEVER raise — always return strings, errors as "Error: ...".
- **Callbacks**: `agent.run_task(text, *, preauthorized, status_cb, ask_user_cb,
  confirm_cb, extra_tools, history)`. `main._run_real_agent` wires these to
  `bridge.update_status` / `bridge.ask_user` / `bridge.confirm` and passes
  `extra_tools=tools_browser.TOOLS`. `tools_browser.configure(confirm, preauth)` and
  `tools_fs.configure(confirm, preauth)` are called per task.
- **Human waits don't count against any task clock** (removed deliberately after a live
  timeout while the user was thinking). Machine time is bounded by MAX_STEPS ×
  MODEL_CALL_TIMEOUT; each ask/confirm bounded by CONFIRM_TIMEOUT (300s, timeout=deny).
- **Telegram is the control channel**: questions and approvals go to the phone even for
  J-badge tasks (the badge panel shows status text only).
- **`num_ctx: 16384` on every Ollama call.** Silent truncation at the 4096 default is the
  classic failure; don't touch this.

## 4. Phase 2 scope — decisions already made with the user (do not reopen)

- **Co-drive**: agent attaches to the user's REAL Edge (their logins; the user navigates
  and stays present; the agent reads the rendered page and fills/clicks visibly in that
  same session). Chosen over agent-owned browser for real sites.
- **Email**: Gmail via the official API, read-only scope, module already exists.
- **Social**: NO POSTING anywhere this build (researched 2026-07: X = pay-per-use credits
  only, no free tier; IG/FB = 2–4 week app review; TikTok unaudited = forced-private
  posts). Reading/summarizing = co-drive only (user navigates to the feed; agent
  summarizes what's on screen). State this honestly in README/demo.
- **Demo centerpiece**: co-drive job application (Greenhouse/Lever-class posting).

### Hard boundaries (user-set; enforce in code AND system prompt)
- CAPTCHA / "verify you are human" / explicit bot check → **stop immediately, tell the
  user to complete it themselves, wait**. Never solve, never automate around. A paused
  demo is correct behavior, not failure.
- NO fingerprint spoofing, proxy rotation, header randomization, or anything that
  disguises automation. If a real logged-in session is still blocked → stop and report;
  route around via an official form/API instead.
- Full draft breakdown before submit (the gate's approval card already lists all form
  values — keep that behavior). Fresh confirmation per consequential action, no standing
  permissions (the only exception is the existing per-task `!` preauth the user types
  themselves).
- Never guess between ambiguous files (resumes/photos) — ask via the phone.

## 5. Work items

### P0.1 — Co-drive launcher (`edge_codrive.bat`, new file)
One double-clickable script: warn/confirm → `taskkill /IM msedge.exe` if running →
relaunch `msedge.exe --remote-debugging-port=9222 --restore-last-session` with the
user's DEFAULT profile (no --user-data-dir override). Echo "Pocket Agent can now
co-drive this browser." Acceptance: after running it, `curl http://127.0.0.1:9222/json/version`
returns JSON.

### P0.2 — CDP attach in `tools_browser.py::_ctx()`
At the top of `_ctx()` (before the owned-Chromium branch): try
`GET http://127.0.0.1:9222/json/version` (aiohttp, ~0.5s timeout). If it responds:
`browser = await _pw.chromium.connect_over_cdp("http://127.0.0.1:9222")`,
`_context = browser.contexts[0]`, pick `_page` = the focused/most recent page
(`_context.pages[-1]`) or `new_page()` if none. Cache a module flag `_cdp = True`.
If not reachable → existing `launch_persistent_context` fallback unchanged.
`shutdown()`: in CDP mode, DISCONNECT (`browser.close()` on a connect_over_cdp handle
closes the connection, not the user's Edge — verify this in a quick test; if it closes
Edge, just drop the reference instead). Never kill the user's browser.
Acceptance: with launcher running, a task's `browser_goto` acts in the user's Edge
window; without it, `python test_browser.py` still passes 25/25 (fallback intact).

### P0.3 — Tab tools (`tools_browser.py`, register in TOOLS)
- `list_tabs()` → numbered list of `_context.pages` (index, title ≤60 chars, url).
- `switch_tab(index)` → set `_page`, `bring_to_front()`, return fresh digest.
- `new_tab(url)` → `_context.new_page()` + goto + digest.
Follow the existing tool-fn pattern exactly (never raise; digest via `_fresh_digest`).
Also: in CDP mode, when a task arrives with "(The user is currently looking at this
page: URL)" (the J badge appends this — see `server.py::_post_task`), the model should
act on that tab. Implement `_page_for_url(url)`: exact-or-prefix match over
`_context.pages`, and have `browser_goto` switch to a matching open tab instead of
navigating a fresh one. Add one system-prompt line telling the model to act on the
user's current tab.

### P0.4 — JS-settle before extraction
`_fresh_digest`/`read_page` call a `_settle(page)` helper: `wait_for_load_state("networkidle", 5s)` + ~800ms. Fixes the live bug where JS-heavy pages extracted before they painted.

### P0.5 — Challenge hard-stop (safety boundary)
`_challenge_on(page)` checks iframe URLs (recaptcha/hcaptcha/turnstile/arkose) and body text ("verify you are human", "unusual traffic", "access denied", …). When true, `_fresh_digest`/`read_page` return a fixed `CHALLENGE_MSG` telling the model to `ask_user` for the user to complete it manually. Never solved or bypassed. Mirrored in SYSTEM_PROMPT.

### P0.6 — Never-guess file picker + user-info capture
`tools_fs.pick_file(hint)` returns one path only when unambiguous, else a numbered list for the model to `ask_user` about; feeds `upload_file`. `tools_fs.remember_fact(key,value)` + `/remember key: value` write `profile_extra.yaml` (gitignored), merged by `read_profile` — so info the user volunteers over Telegram is reused, not re-asked.

---

## BUILD STATUS (this session)
**Track A (all P0 items above) is IMPLEMENTED** in `tools_browser.py`, `tools_fs.py`,
`agent.py`, `gate.py`, `config.py`, plus `edge_codrive.bat`. Verified: fs/state/browser
regression suites green (25/25 on the mock form via the owned-Chromium fallback); CDP
attach + live co-drive still need a run with `edge_codrive.bat` started and the real phone.
**Track B (Gmail)** is wired (`/inbox`, registry merge) and waits only on the user's
`credentials.json`. **Track C (Reddit + YouTube read-only search, `/news`)** is wired
(`tools_social.py`, registry merge — see `SOCIAL_SETUP.md`); social feed reading (X/IG/
TikTok/FB) stays co-drive-only per the locked scope decision above, so it needs no
dedicated tool. **Track B-Outlook** is still teammate work — see the "Team work split"
section of `README.md` for the ready-to-grab spec and the integration contract every
track follows. This doc is the archival design rationale; README is the current source
of truth for who-builds-what.

(Original archived reference `_context.pages` note above is superseded by the shipped code.)
`_context.pages`; add one system-prompt line telling the model t