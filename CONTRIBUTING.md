# Contributing / Team Setup

Onboarding doc for teammates working on this repo. For what the project does and
the demo script, see [README.md](README.md). For current build status and the
roadmap, see [PHASE2_BUILD_PLAN.md](PHASE2_BUILD_PLAN.md).

## 1. Clone

```
git clone https://github.com/jushayden/JerryAI.git
cd JerryAI
```

## 2. Prerequisites

- Python 3.11+
- [Ollama](https://ollama.com/download) — local model runtime, no API keys/cloud calls
- A Telegram account (to create a bot via @BotFather)
- Windows, macOS, or Linux — paths in code assume Windows-style but the stack is
  cross-platform; ask in the group chat if you hit a path issue on Mac/Linux

## 3. Install

```
pip install -r requirements.txt
python -m playwright install chromium
ollama pull qwen3-coder:30b
```

`qwen3-coder:30b` is the pinned primary model. If your machine can't run a 30B model,
pull a fallback (`gpt-oss:20b` or `qwen3:14b`) and set `MODEL=` in `.env` accordingly.

## 4. Secrets — every one of these is gitignored, never commit them

| File | How to create it | Needed for |
|---|---|---|
| `.env` | `copy .env.example .env`, paste your own `BOT_TOKEN` from @BotFather | Telegram bot |
| `profile.yaml` | `copy profile.example.yaml profile.yaml`, fill in your real info | form-filling demo |
| `credentials.json` / `token.json` | follow [GMAIL_SETUP.md](GMAIL_SETUP.md) | Gmail inbox tool (optional) |

Each teammate needs their **own** `BOT_TOKEN` (own bot from BotFather) to run and test
locally without stepping on each other. Don't share tokens over Slack/Discord in plaintext
— paste them into `.env` directly, or DM if you must.

If you ever paste a real secret into a commit, chat, or PR by mistake: rotate/revoke it
immediately (BotFather `/revoke`, or regenerate the Google OAuth client) rather than just
deleting the message — assume it's compromised the moment it's posted anywhere.

## 5. Run it

```
python main.py
```

Starts the mock-form server (`:8000`), warms up the model, and starts Telegram polling.
On your phone: message your bot, `/start` once, then just send it tasks. See README's
"Demo script" section for example tasks to try.

## 6. Tests

```
python test_state.py
python test_tools_fs.py
python test_tools_email.py   # offline — stubs the Gmail API, no account needed
python test_browser.py       # opens a visible Chromium window
```

Run these before opening a PR if you touched `state.py`, `tools_fs.py`, `tools_email.py`,
or `tools_browser.py`.

## 7. Code conventions (keep it lean — hackathon project)

- Plain module-level async functions. No classes beyond dataclasses, no frameworks
  beyond what's already in `requirements.txt`.
- New PC/file actions go in `tools_fs.py` and must go through `_safe()` sandboxing.
- New browser actions go in `tools_browser.py`.
- Anything irreversible (submit, delete, send, buy) must be routed through `gate.py`'s
  approval flow — don't let the model self-authorize destructive actions.
- Keep tool results small (`config.TOOL_RESULT_MAX`) — the model's context is the
  bottleneck, not disk/network.

## 8. Git workflow

- Branch off `main`: `git checkout -b yourname/short-feature-name`
- Small, focused commits with a clear message
- Open a PR into `main` before merging — even solo, it gives the rest of the team a diff
  to skim
- Don't force-push shared branches

## 9. Where to look

- **README.md** — architecture, setup, demo script, demo-day checklist
- **PHASE2_BUILD_PLAN.md** — what's verified working vs. unproven, known gaps, next steps
- **GMAIL_SETUP.md** — one-time Google OAuth setup for the inbox tool
- Questions / blocked on something → ask in the team chat before spending >30 min stuck;
  this is a time-boxed hackathon build.
