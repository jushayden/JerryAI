# Social setup — Reddit + YouTube (one-time, ~5 minutes)

`reddit_search`, `reddit_feed`, and `youtube_search` are **read-only** and use
each platform's official free API. Both are optional — the agent tells you
what's missing if you ask it to use a platform that isn't set up yet.

Per the team's Phase 2 scope decision (see `PHASE2_BUILD_PLAN.md`), this build
does **no posting** on any platform, and X/Instagram/TikTok/Facebook reading
stays co-drive only (open the feed in Edge, ask the agent to summarize what's
on screen) rather than a scraper or API integration.

## Reddit

1. Go to <https://www.reddit.com/prefs/apps> → **create another app…**
2. Type: **script**. Name: anything. Redirect URI: `http://localhost:8080`.
3. Add to `.env`:
   ```
   REDDIT_CLIENT_ID=<the string under the app name>
   REDDIT_CLIENT_SECRET=<the "secret" field>
   REDDIT_USER_AGENT=pocket-agent:v1 (by /u/your_username)
   ```

No OAuth flow, no browser prompt — this is a read-only "script" app credential,
works immediately.

## YouTube Data API v3

1. <https://console.cloud.google.com/> → sign in → create/select a project
   (reuse the same one from `GMAIL_SETUP.md` if you already made one).
2. Menu → **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.
3. Menu → **APIs & Services → Credentials → Create Credentials → API key**.
4. Add to `.env`:
   ```
   YOUTUBE_API_KEY=<the key>
   ```

No consent screen, no scopes — a plain API key is enough for public search.

## Quota note

YouTube's free quota is 10,000 units/day; each `youtube_search` call costs 100
units (~100 searches/day). Reddit has no meaningful rate limit for this use.

## Notes

- **Secrets:** these three values live in `.env` only — already gitignored,
  never commit them.
- **Wrong/missing values:** if a tool returns
  `Error: Reddit is not set up: REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET missing...`
  or the YouTube equivalent, double-check the `.env` keys above are filled in
  and restart `python main.py` (env vars load once at startup).
- **`/news`** reads your `interests:` list from `profile.yaml` and doesn't need
  either of these — it uses the existing browser tools against DuckDuckGo's
  HTML search instead.
