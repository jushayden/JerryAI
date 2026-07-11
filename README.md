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
                                                  ├─ gate.py           deterministic approval gate
                                                  └─ state.py          briefs + events.jsonl audit log
```

**Safety model:** filling/drafting is autonomous; **irreversible actions are gated.** Any
click that would submit/send/buy/delete triggers an approval card on your phone (Approve/
Deny) — enforced by rules in code, not the model's judgment. The agent works only in a
browser session **you** drive; it never bypasses CAPTCHAs, bot checks, paywalls, logins, or
ToS, and never spoofs its traffic — if a site blocks a real logged-in session, it stops and
tells you. Prefix a task with `!` to pre-authorize (gate logs but doesn't block).

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

### Track C — Social + news briefing (read-only)  (Reddit/YouTube/News ✅ wired)
- **Reddit + YouTube** (`tools_social.py`): official **read-only** APIs — `reddit_search`,
  `reddit_feed`, `youtube_search`. No OAuth, just an API key/app secret each — follow
  **[SOCIAL_SETUP.md](SOCIAL_SETUP.md)**. This is the reference for platforms with a free
  read API, same integration contract as `tools_email.py`.
- **News**: a `/news` command — no new tool, just a canned task using the existing browser
  tools (DuckDuckGo HTML → read top sources) over an `interests:` list in your profile.
- **Social reading (X/IG/TikTok/FB)**: co-drive only — you open the feed in Edge, hit the J
  badge with "summarize what's new", the agent reads the rendered page. Stops if a platform
  blocks it. (No free official read API for these, so no dedicated tool — this is the
  scoped, deliberate design, not a gap.)
- **Posting is OUT OF SCOPE, all platforms** (researched Jul 2026): X has no free tier
  (pay-per-use credits only); Instagram/Facebook need 2–4-week app review; TikTok unaudited
  forces posts to private. If a real posting API becomes reachable, it must use a fresh
  per-post confirmation showing platform, exact text, media, tags, and visibility.

---

## Setup (once)

1. Install [Ollama](https://ollama.com/download) (latest — RTX 50-series needs recent builds):
   `ollama pull qwen3-coder:30b` (primary; fallbacks `gpt-oss:20b`, `qwen3:14b`).
2. `pip install -r requirements.txt` and `python -m playwright install chromium`.
3. Telegram: **@BotFather** → `/newbot` → `copy .env.example .env`, paste `BOT_TOKEN=`.
4. `copy profile.example.yaml profile.yaml`, fill in your info (gitignored). Add
   `resume_path:` and `interests:` if you want uploads and news.
5. *(Optional, for `/inbox`)* Gmail OAuth via [GMAIL_SETUP.md](GMAIL_SETUP.md).
6. *(Optional, for Reddit/YouTube search)* [SOCIAL_SETUP.md](SOCIAL_SETUP.md).

## Run

```
python main.py
```

Starts the mock-form server (:8000), warms the model, starts the bot + J-badge endpoint.
On your phone: `/start` once (registers you as owner), then just text tasks.

- `/inbox` — email briefing (needs Gmail OAuth)
- `/news` — briefing over your `interests:` list (no setup needed beyond `profile.yaml`)
- Just ask in plain language for Reddit/YouTube: "search reddit for X", "what's hot in r/LocalLLaMA", "find youtube videos about Y" (needs [SOCIAL_SETUP.md](SOCIAL_SETUP.md) keys)
- `/remember key: value` — teach the agent a fact to reuse (e.g. `/remember work authorization: US citizen`)
- `/brief` · `/status` · `/cancel` · `/testconfirm`
- `!task text` — pre-authorized (skips approval gates)

## Co-drive your real browser (the app-filler demo)

For real sites (job applications, signups) the agent works **inside your own Edge session**
so your logins apply and nothing is spoofed:

1. Double-click **`edge_codrive.bat`** — it reopens Edge with a debug port (your tabs are
   restored). Do this once per session.
2. Navigate to the page yourself (e.g. a Greenhouse/Lever job posting) and stay there.
3. From the phone or the **J badge** (load once: `edge://extensions` → Developer mode →
   Load unpacked → `edge_extension/`): "fill this application using my profile."
4. Watch fields fill in your own tab. Unknown field → it asks you on Telegram. Resume field
   → it finds/asks for the file. **Submit → approval card on your phone with every value.**
5. Hit a CAPTCHA? It **stops and asks you** to complete it, then continues. It will not
   solve or bypass one — a paused task is correct.

If you skip the launcher, the agent falls back to its own Chromium (fine for the mock form
and cooperative sites; weaker on bot-shielded ones).

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
- [ ] Honest limits to say first: no social posting (platform review/cost); closed
      shadow-DOM forms invisible; the gate is rules-first, not airtight; the agent stops at
      CAPTCHAs by design (it won't solve them).

## Tests

`python test_state.py` · `python test_tools_fs.py` · `python test_browser.py`
(browser test opens a visible Chromium window and drives the mock form end to end)
