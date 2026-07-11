# Pocket Agent

Local-first agentic AI that runs on your PC, reasons on a **local model** (Ollama — no
cloud API anywhere in the loop), and is **controlled from your phone** via a Telegram
bot: send a task, the PC does it (files, apps, browser, form-filling from your saved
profile), your phone gets a report with a proof screenshot.

## Architecture

```
phone (Telegram, any network) ⇄ Telegram servers ⇄ long polling ⇄ bridge.py
                                             task queue (serial) → agent.py (Ollama tool loop)
                                                  ├─ tools_fs.py      files, apps, screenshots
                                                  ├─ tools_browser.py Playwright headed Chromium,
                                                  │                    generic form-filling
                                                  ├─ gate.py          deterministic approval gate
                                                  └─ state.py         briefs + events.jsonl audit log
```

Safety model: **filling/drafting is autonomous; irreversible actions are gated.**
Any click that would submit/send/buy/delete triggers an approval card on your phone
(Approve/Deny buttons) — enforced by rules in code, not by the model's judgment.
Prefix a task with `!` to pre-authorize it (gate logs but doesn't block).

## Setup (once)

1. Install [Ollama](https://ollama.com/download) (latest — RTX 50-series needs recent builds), then:
   `ollama pull qwen3-coder:30b` (primary; also pull fallbacks `gpt-oss:20b`, `qwen3:14b`)
2. `pip install -r requirements.txt` and `python -m playwright install chromium`
3. Telegram: message **@BotFather** → `/newbot` → copy the token.
   `copy .env.example .env` and paste it into `BOT_TOKEN=`.
4. `copy profile.example.yaml profile.yaml` and fill in your real info
   (gitignored; the agent reads it via the `read_profile` tool).

## Run

```
python main.py
```

Starts the mock-form server (localhost:8000), checks + warms up the model, starts the
bot. Then on your phone: `/start` once (registers you as owner), and just text it tasks.

- `/brief` — digest: done / running / needs-you / queued
- `/status` — one-liner
- `/cancel` — abort the current task, clear pending questions
- `/testconfirm` — round-trip test of the approval buttons
- `!task text` — pre-authorized (skips approval gates)

## Edge "J" badge (in-browser prompt)

A floating J button on every page (like Grammarly), wired to the same agent:
type a prompt, it sends it plus the current page's URL to `localhost:8765`, the agent
acts (opening its own Chromium on that page when the task needs it), live status shows
in the panel, and the phone still gets the report. Load once: `edge://extensions` →
Developer mode → **Load unpacked** → select `edge_extension/`. The endpoint binds to
127.0.0.1 only and requires the token in `config.LOCAL_TOKEN` (override via `LOCAL_TOKEN`
in `.env`; keep the extension's `content.js` TOKEN in sync if you change it).

## Demo script (~5 min)

1. "Make a folder called judges_demo on the Desktop and write a file inside it called
   hello.md with a 3-line pitch of this project." — autonomous file op + report + screenshot.
2. "Open Notepad." — instant PC control.
3. "Register me for the DevFest conference at http://localhost:8000/index.html using my
   profile." — watch fields fill themselves; phone buzzes with the approval card listing
   the exact values; tap Deny first (graceful wrap-up), rerun and Approve, or rerun with
   `!` to show pre-authorization. Finish with `/brief`.

## Demo-day checklist

- [ ] `ollama ps` shows the model 100% on GPU after warm-up
- [ ] Timed rehearsal: form-fill task under ~2 min (if not: set `MODEL=gpt-oss:20b` in `.env`,
      `ollama stop qwen3-coder:30b`, restart — rehearse this swap, don't improvise it)
- [ ] Full run once with the phone on **cellular** (proves cross-network control)
- [ ] Rehearse `/cancel` mid-task once
- [ ] PC internet: know your hotspot fallback if venue wifi blocks Telegram
- [ ] Known limits (say them before judges find them): closed shadow-DOM forms invisible;
      file-upload fields unsupported; gate is deterministic-rules-first, not airtight —
      a submit button labeled something exotic outside a <form> can slip past; bot-detecting
      sites may misbehave with automated Chromium.

## Tests

`python test_state.py` · `python test_tools_fs.py` · `python test_browser.py`
(browser test opens a visible Chromium window and drives the mock form end to end)
