# Pocket Agent

Local-first agentic AI that runs on your PC, reasons on a **local model** (Ollama — no
cloud API anywhere in the loop), and is **controlled from your phone** via a Telegram
bot: send a task, the PC does it (files, apps, browser, form-filling from your saved
profile), your phone gets a report with proof.

> New to this repo? Start with **[CONTRIBUTING.md](CONTRIBUTING.md)** for team setup,
> secrets handling, and git workflow. Design rationale + build status lives in
> **[PHASE2_BUILD_PLAN.md](PHASE2_BUILD_PLAN.md)**.

## Architecture

```
phone (Telegram, any network) ⇄ Telegram servers ⇄ long polling ⇄ bridge.py
   Edge "J" badge ⇄ 127.0.0.1:8765 ⇄ server.py ─────────────────────┘
                                             task queue (serial) → agent.py (Ollama tool loop)
                                                  ├─ tools_fs.py       files, apps, screenshots, file-picker
                                                  ├─ tools_browser.py  Playwright — co-drive your real Edge
                                                  ├─ tools_email.py    Gmail read-only (scan_inbox)
                                                  ├─ tools_system.py   volume/brightness/lock/media/power (gated)
                                                  ├─ gate.py           deterministic approval gate
                                                  └─ state.py          task origins, sources, artifacts + audits
```

**Safety model:** filling, research, scraping, and drafting are autonomous; **high-impact
actions are always gated.** Submitting, sending/posting, buying/booking/paying, deleting,
and account changes trigger an exact approval card on your phone. CAPTCHA, bot checks, and
paywalls are never bypassed. Passwords and OTPs can be requested through Telegram as
one-use in-memory handles; raw values never enter the model, audit, or status messages.

---

## Team work split — three tracks, one Telegram bot

All three tracks plug into the **same** bot via one integration contract, so they can be
built in parallel and merged independently.

### The integration contract (how any track connects)
1. **Tool module** `tools_<x>.py` exposing
   `TOOLS: dict[name, {"schema": <Ollama function schema>, "fn": async (args: dict) -> str}]`.
   Tool fns **never raise** — they return strings; errors as `"Error: ..."`.
2. **Register** by merging your `TOOLS` into `extra_tools` in
   [main.py](main.py)`::_run_real_agent` (already merges `tools_browser` + `tools_email`).
3. **Command** in [bridge.py](bridge.py): a `/command` handler that calls
   `state.new_task("<canned instruction>")` — you inherit the whole queue → agent loop →
   live status card → phone report for free. (`/inbox` is the worked example — copy it.)

Reuse, don't reinvent: `ask_user`/`confirm` (bridge), `gate.py` approval, `state` briefs.

### Track A — Browser-agentic / application filler  ✅ built (owner: core)
Co-drives **your real Edge** session: opens/switches tabs, reads the visible page, fills any
form from your profile + facts you give over Telegram, asks when unsure, uploads files you
choose, and gates every submit. Files: `tools_browser.py`, `tools_fs.py`, `agent.py`,
`gate.py`, `edge_codrive.bat`.

### Track B — Email briefing  (Gmail ✅ wired · Outlook ⛏ teammate)
- **Gmail** (`tools_email.py`, `/inbox`): official **read-only** API. Needs one-time OAuth —
  follow **[GMAIL_SETUP.md](GMAIL_SETUP.md)**, drop `credentials.json` in the repo root.
  This is the reference implementation of the integration contract.
- **Outlook** (teammate): new `tools_email_outlook.py` via Microsoft Graph (`Mail.Read`,
  MSAL device-code auth), same `scan_inbox`-shaped tool + `OUTLOOK_SETUP.md`, folded into
  `/inbox`. Heads-up: the Azure app registration is the time risk — start it early.

### Track C — Social + news briefing  (browser tools built)
- **News**: a `/news` command — no new tool, just a canned task using the existing browser
  tools (DuckDuckGo HTML → read top sources) over an `interests:` list in your profile.
- **Social reading**: co-drive only — you open your X/IG/TikTok/FB feed in Edge, hit the J
  badge with "summarize what's new", the agent reads the rendered page. Stops if a platform
  blocks it.
- **Posting through the visible Edge UI** is high-impact and always requires a fresh
  Telegram approval showing the destination and exact content. Jerry never bypasses
  platform controls.

---

## Setup (once)

1. Install [Ollama](https://ollama.com/download) (latest — RTX 50-series needs recent builds):
   `ollama pull qwen3-coder:30b` and `ollama pull qwen3-vl:30b-a3b-instruct`
   (higher-accuracy local visual perception; the models are swapped in memory).
2. `pip install -r requirements.txt` and `python -m playwright install chromium`.
3. Telegram: **@BotFather** → `/newbot` → `copy .env.example .env`, paste `BOT_TOKEN=`.
4. `copy profile.example.yaml profile.yaml`, fill in your info (gitignored). Add
   `resume_path:` and `interests:` if you want uploads and news.
5. *(Optional, for `/inbox`)* Gmail OAuth via [GMAIL_SETUP.md](GMAIL_SETUP.md).

## Run

```
python main.py
```

Starts the mock-form server (:8000), warms the model, starts the bot + J-badge endpoint.
On your phone: `/start` once (registers you as owner), then just text tasks.

- `/inbox` — email briefing (needs Gmail OAuth)
- `/remember key: value` — teach the agent a fact to reuse (e.g. `/remember work authorization: US citizen`)
- `/brief` · `/status` · `/cancel` · `/testconfirm`

## Control your PC from your phone

Plain-language tasks drive the machine directly (`tools_system.py`): *"set the volume to 20%"*,
*"mute"*, *"turn the brightness down"*, *"pause the music"*, *"lock my PC"*. **Power actions**
— *"sleep the PC"*, *"shut down in 5 minutes"*, *"restart"* — are **gated**: you get an approval
card on your phone first (say *"cancel the shutdown"* to abort a countdown). Pairs with the
approval model and, if you have the scheduler, with timed jobs (e.g. sleep the PC at 11pm).

> Windows-only. Volume needs `pycaw`, brightness needs `screen-brightness-control` (both
> installed by `pip install -r requirements.txt` on Windows). External monitors need DDC/CI
> support for brightness; if a call isn't supported it reports the error rather than failing silently.

## Co-drive your real browser (the app-filler demo)

For real sites, the agent works inside a persistent **Jerry Edge profile**. Modern Edge
rejects remote debugging on the default profile, so sign into required sites once in this
dedicated profile; its cookies, logins, and tabs persist between sessions:

1. Optionally double-click **`edge_codrive.bat`** to enable co-drive ahead of time. If Edge
   is closed, Jerry starts it automatically. If it is already open without co-drive, Jerry
   asks on Telegram before closing and restoring it.
2. Navigate to the page yourself (e.g. a Greenhouse/Lever job posting) and stay there.
3. From the phone or the **J badge** (load once: `edge://extensions` → Developer mode →
   Load unpacked → `edge_extension/`): "fill this application using my profile."
4. Watch fields fill in your own tab. Unknown field → it asks you on Telegram. Resume field
   → it finds/asks for the file. **Submit → approval card on your phone with every value.**
5. Hit a CAPTCHA? It **stops and asks you** to complete it, then continues. It will not
   solve or bypass one — a paused task is correct.

Production tasks never silently fall back to a separate browser profile. Set
`BROWSER_MODE=owned` only for development and automated browser tests.

## Remote browser work from Telegram

With `python main.py` running and the PC awake, send a research, itinerary, scraping, or
form task directly to Telegram. Jerry opens task-owned Edge tabs, edits one live status
card, asks for missing information or high-impact approvals, and finishes with a sourced
summary, screenshot, generated data/download artifacts, and a timestamped audit file.

## Demo script (~6 min)

1. "Make a folder called judges_demo on the Desktop with hello.md — a 3-line pitch." —
   autonomous file op + report + on-disk verification.
2. "Open Notepad." — instant PC control.
3. **Co-drive job application** (centerpiece): run `edge_codrive.bat`, open a real posting,
   J-badge "fill this using my profile, don't submit yet." Watch it fill, ask a clarifying
   question, attach your resume; then approve the submit from your phone.
4. `/inbox` — local model reads your Gmail and briefs you, flagging what matters.
5. `/brief` — the session digest.

## Demo-day checklist

- [ ] `ollama ps` shows the model on GPU after warm-up
- [ ] Timed rehearsal under ~2 min (else `MODEL=gpt-oss:20b` in `.env`, `ollama stop qwen3-coder:30b`, restart — rehearse the swap)
- [ ] One full run with the phone on **cellular** (proves cross-network control)
- [ ] `edge_codrive.bat` run + one real co-drive form fill incl. resume upload
- [ ] Rehearse `/cancel` mid-task; know your hotspot fallback if venue wifi blocks Telegram
- [ ] Gmail OAuth done + `/inbox` returns a real summary
- [ ] Honest limits to say first: closed shadow-DOM controls may be inaccessible; visual
      fallback is best-effort; the gate is rules-first; the agent stops at
      CAPTCHAs by design (it won't solve them).

## Tests

`python test_state.py` · `python test_tools_fs.py` · `python test_tools_system.py` ·
`python test_remote_operator.py` · `python test_vision.py` · `python test_browser.py`
(system test is offline — stubs the hardware calls; browser test opens a visible Chromium
window and drives the mock form end to end)

After pulling the vision model, run `python test_vision.py --real` for the local-model smoke test.
Run `python test_live_vlm.py` for the 30B VLM check against a complex live Microsoft page.
For the selected real application test, complete any human verification in co-drive Edge,
then run `python test_real_site.py --co-drive`; it fills and clears synthetic values on
`https://job-boards.greenhouse.io/claudecorps/jobs/4250200009` and never submits.
