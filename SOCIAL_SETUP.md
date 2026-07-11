# Social platform setup

Every platform is optional — the agent tells you what's missing when you ask
it to use one. Check readiness anytime with `python main.py --status`.

## Telegram (the remote control — do this first)

1. Message **@BotFather** in Telegram → `/newbot` → pick a name and username.
2. Copy the token it gives you → `TELEGRAM_BOT_TOKEN` in `.env`.
3. Message **@userinfobot** → it replies with your numeric user ID →
   `TELEGRAM_OWNER_ID` in `.env`. Only this account can command the bot.
4. Open a chat with your new bot and press Start once.

## Reddit (free, reliable — recommended first platform)

1. Go to <https://www.reddit.com/prefs/apps> → **create another app…**
2. Type: **script**. Name: anything. Redirect URI: `http://localhost:8080`.
3. Copy the values into `.env`:
   - `REDDIT_CLIENT_ID` — the string under the app name
   - `REDDIT_CLIENT_SECRET` — the "secret" field
   - `REDDIT_USER_AGENT` — e.g. `windows:jerryai:v0.1 (by /u/yourname)`
4. Optional, only if you want the agent to be able to **post** to Reddit:
   `REDDIT_USERNAME` and `REDDIT_PASSWORD`. Posts use the format
   `r/subreddit: title | body` and always require an approval card.

## YouTube (free, read-only)

1. <https://console.cloud.google.com> → create a project.
2. **APIs & Services → Library** → enable **YouTube Data API v3**.
3. **APIs & Services → Credentials → Create credentials → API key**.
4. Put the key in `.env` as `YOUTUBE_API_KEY`.

Quota: 10,000 free units/day; each search costs 100. The agent caches
identical searches for 10 minutes to stretch this.

## X / Twitter (free tier = posting only)

1. <https://developer.x.com> → sign up for a **Free** developer account.
2. Create a project + app. In the app's **User authentication settings**,
   enable **Read and write**.
3. **Keys and tokens** page — copy all four into `.env`:
   - `X_API_KEY`, `X_API_SECRET` (consumer keys)
   - `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET` (regenerate AFTER enabling
     read/write, or posting will 403)
4. Free tier reality check: `create_tweet` works (~500 posts/month); **search
   and timelines do not** — they need the paid Basic tier. The agent will say
   exactly that if you ask it to search X.

## Instagram (browser session — no API)

There is no usable official API for personal accounts, so the agent drives a
real Chromium with your own logged-in session. **This violates Instagram's
ToS — read the risk section in README.md first.**

1. Run:
   ```powershell
   .venv\Scripts\python setup_instagram.py
   ```
2. A browser window opens on instagram.com. Log in normally (2FA works).
3. Press Enter in the console once your feed is visible. The script verifies
   the session and saves a marker; cookies persist in `sessions/instagram/`.
4. Done — no keys needed. If Instagram ever challenges the session, the agent
   stops and asks you to run this script again. It never retries by itself.
