# Gmail setup (one-time, ~5 minutes)

The Gmail tools let the agent **read your inbox and send, reply, draft, archive, and
trash** email on your behalf (scope `gmail.modify` — it cannot permanently delete;
trashed mail is recoverable). Sending, replying, and trashing are gated: the agent asks
you to approve on your phone first. It needs a Google OAuth "Desktop app" client. Works
with a personal @gmail.com account — no verification, no billing.

## 0. Install the libraries (if not already done)

```
pip install --user google-api-python-client google-auth-oauthlib
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

> **Upgrading from an old read-only setup?** If you already had `token.json` from when
> the tool was read-only, **delete `token.json`** and run once more — the added scopes
> won't take effect until you re-consent, and you'll otherwise get an "insufficient
> permission" error the first time you try to send.

## Notes

- **Secrets:** `credentials.json` and `token.json` now grant read **and send/modify**
  access to your account — treat them like a password. Keep them private and make sure
  both are listed in `.gitignore` before committing anything.
- **Token expiry:** while the consent screen is in Testing mode, Google expires
  the refresh token after ~7 days. If `/inbox` starts failing with an auth error,
  delete `token.json` and run it again to re-consent.
- **Wrong/missing file:** if you see `Error: Gmail is not set up: ...credentials.json not found`,
  the JSON from step 4 isn't at the exact path above.
