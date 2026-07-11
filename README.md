# JerryAI — Pocket Agent

A personal agent you command from your phone. Text the Telegram bot in plain
language — *"What's new with the Tesla Model 2?"*, *"Post 'hello' to my X"*,
*"Show me my Instagram feed"* — and a local Ollama-powered tool loop does the
work on **your PC with your own accounts**, sending digests, links, images and
proof screenshots back to Telegram.

Anything that **writes** (posting, deleting, form submits) first sends an
Approve / Deny card to your phone showing the exact pending action. Everything
is audited to `state/events.jsonl`.

## Setup

```powershell
# 1. Python env (3.12 recommended)
py -V:Astral\CPython3.12.13 -m venv .venv          # or any Python 3.12
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium

# 2. Ollama (local LLM with native tool calling)
#    Install from https://ollama.com/download, then:
ollama pull qwen2.5        # or llama3.1 / llama3.3 / mistral-nemo

# 3. Keys
copy .env.example .env     # fill in what you have — everything is optional
copy profile.example.yaml profile.yaml
```

Per-platform API keys: see **[SOCIAL_SETUP.md](SOCIAL_SETUP.md)**.
Check what's ready at any time:

```powershell
.venv\Scripts\python main.py --status
```

Windows tip: run with `PYTHONUTF8=1` set (`$env:PYTHONUTF8 = '1'`) so emoji in
social content never trip the console encoding.

## Running

```powershell
.venv\Scripts\python main.py                    # Telegram bot mode
.venv\Scripts\python main.py --cli "task"       # no Telegram needed; approvals in console
.venv\Scripts\python main.py --cli-yes "task"   # auto-approve gated actions (careful)
```

In Telegram:

- Send any task in plain language.
- Prefix a task with `!` to pre-authorize writes for that one task (no card).
- `/status` — platform readiness · `/cancel` — abort the current task.
- Only your `TELEGRAM_OWNER_ID` can command the bot; everyone else is ignored.

## What the agent can do

| Tool | Gated? |
|---|---|
| `social_search` / `social_get_feed` / `social_get_profile` | no |
| `social_post` | **yes — approval card** |
| `send_social_digest` (formatted results + thumbnails to Telegram) | no |
| `fs_read_file` / `fs_write_file` / `fs_list_dir` (sandboxed to `workspace/`) | no |
| `fs_delete` | **yes** |
| `browser_extract` / `browser_screenshot` | no |
| `browser_fill_and_submit` | **yes** |

Platform coverage:

- **Reddit** — search, feed (your subreddits from `profile.yaml`), profile, post (needs username/password creds).
- **YouTube** — search, trending feed, channel profiles. Read-only.
- **X/Twitter** — post + profile via the official API **free tier**. Search and
  timelines need the paid Basic tier and will say so instead of failing silently.
- **Instagram** — feed, hashtag search, profiles via your own logged-in browser
  session (run `python setup_instagram.py` once). Posting via automation is
  **disabled by design**.

## Tests

```powershell
.venv\Scripts\python -m pytest              # 66 offline tests, no keys needed
$env:RUN_LIVE_TESTS = '1'; .venv\Scripts\python -m pytest tests\live  # real APIs
```

## Known risks — read this

- **Instagram automation violates Instagram's ToS.** Accounts can be
  challenged or banned. Mitigations built in: read-mostly usage (no automated
  posting/liking/following), hard throttle (30 actions/hour + human-like
  random delays), a persistent real-browser session, and actions only ever run
  when you request them. If Instagram challenges the session the agent stops
  immediately, sends you a screenshot, and **never retries** — you re-login
  manually with `setup_instagram.py`.
- **X free tier cannot search.** Either pay for Basic or use Reddit/YouTube
  for search; the agent tells you which path it can take.
- **DOM scrapers break when platforms redesign.** Policy: fail loudly with a
  screenshot sent to your phone ("layout may have changed"), never guess.
- All secrets stay in `.env`; browser sessions in `sessions/`; both gitignored
  along with `workspace/`, `downloads/` and `state/`.
