# Gmail setup (one-time, ~5 minutes)

The Gmail tools let the agent **read your inbox and send, reply, draft, and trash** email
on your behalf (scope `gmail.modify` — it cannot permanently delete; trashed mail is
recoverable). Sending, replying, and trashing are gated: the agent asks you to approve on
your phone first. It needs a Google OAuth "Desktop app" client. Works with a personal
@gmail.com account — no verification, no billing.

## 0. Install the libraries

These ship in `requirements.txt`, so `pip install -r requirements.txt` already covers them.
If you set the project up before they were added, run:

```
pip install google-api-python-client google-auth-oauthlib
```

## 1. Create a Google Cloud project

1. Go to <https://console.cloud.google.com/> and sign in with your Gmail account.
2. Top bar → project picker → **New Project** → name it e.g. `pocket-agent` → **Create**
   (and make sure it is the selected project).

## 2. Enable the Gmail API

1. Menu → **APIs & Services → Library**.
2. Search **Gmail API** → open it → **Enable**.

## 3. Configure the OAuth consent screen

1. Menu → **APIs & Services → OAuth consent screen** (a.k.a. "Google Auth Platform").
2. User type: **External** → Create. App name e.g. `Pocket Agent`, pick your email
   for support/developer contact. Save through the steps (scopes can be skipped).
3. Under **Audience / Test users**: **Add users** → add **your own Gmail address**.
   Leave publishing status as **Testing** — no Google verification needed.

## 4. Create the Desktop OAuth client and download it

1. Menu → **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
2. Application type: **Desktop app** → any name → **Create**.
3. **Download JSON** and save it as exactly:

   ```
   c:\school\Hackathon2026\credentials.json
   ```

## 5. First use

The first time the agent runs `/inbox` (the `scan_inbox` tool), a browser window
opens **once**:

1. Pick your Gmail account.
2. On "Google hasn't verified this app" click **Continue** (or Advanced → Go to
   Pocket Agent). This appears because the app is in Testing mode — that's fine.
3. Allow the requested Gmail access (**read, compose, send, and modify** — the agent
   still can't permanently delete). Tick the box(es) and continue.

A `token.json` is then saved next to `credentials.json` and reused from then on —
no more browser prompts.

> **Upgrading from an old read-only setup?** Nothing to do by hand. If your existing
> `token.json` only has the read-only scope, the agent notices, discards it, and re-opens
> the consent browser **once** automatically the next time a Gmail tool runs — approve it
> and you're on the new scope. (No manual delete, no "insufficient permission" error.)

## Notes

- **Secrets:** `credentials.json` and `token.json` now grant read **and send/modify**
  access to your account — treat them like a password. Keep them private and make sure
  both are listed in `.gitignore` before committing anything.
- **Stop the weekly re-consent (recommended):** while the consent screen is in **Testing**
  mode, Google expires the refresh token after ~7 days, so you'd have to re-approve weekly.
  To avoid that, go to **OAuth consent screen → Publishing status → Publish app** (set it to
  *In production*). For personal use you do **not** need Google's verification — you'll still
  see the one-time "unverified app" warning (click through it), but the token stops expiring.
- **If auth ever fails anyway** (revoked access, etc.), the agent just re-opens the consent
  browser once on the next run — you don't need to touch `token.json` yourself.
- **Wrong/missing file:** if you see `Error: Gmail is not set up: ...credentials.json not found`,
  the JSON from step 4 isn't at the exact path above.
