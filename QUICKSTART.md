# Tora AI quick start

Tora is a local-first Windows agent. Ollama runs the reasoning model on your PC; Playwright works in a visible Edge session; Telegram carries requests, questions, approvals, and reports. The source package includes the Edge extension and a local-only pairing key. Never put a Telegram bot token or `.local-token` in a website, GitHub issue, or commit.

## Install

1. Install Python 3.11+, Ollama, and Microsoft Edge.
2. In PowerShell, open this folder and run:

   ```powershell
   python scripts/setup.py
   ```

   The helper creates `.venv`, installs Python dependencies, installs the Playwright browser, copies `.env.example` to `.env`, and creates `.local-token`.

3. Open `.env` and replace the placeholder `BOT_TOKEN` with the token from BotFather. Leave `ALLOWED_CHAT_ID=0` for the first pairing.
4. Pull a model in Ollama:

   ```powershell
   ollama pull qwen3-coder:30b
   ```

   If your machine needs a smaller model, pull one of the fallbacks and set `MODEL=` in `.env`.

## Pair Telegram

Start Tora:

```powershell
.venv\Scripts\python.exe main.py
```

Send `/start` to your bot. Tora replies with your chat ID; it does not silently claim ownership. Stop Tora, set `ALLOWED_CHAT_ID=<your chat ID>` in `.env`, and start it again. Send `/start` once more to confirm that Tora is ready.

## Pair the Edge extension

1. Open `edge://extensions`, enable **Developer mode**, choose **Load unpacked**, and select this folder's `edge_extension` directory.
2. Open the extension's **Details → Extension options** page.
3. Open `.local-token` in Notepad and paste the complete key into the Options page. Choose **Save & check connection**.
4. Start `main.py`, open an ordinary `http://` or `https://` page, and use the T badge. The badge sends only a task and the current page URL to Tora's loopback service. Approvals and full reports arrive in Telegram.

## Try it

- In Telegram: `Create a folder called tora_demo on my Desktop and write hello.md with a short note.`
- In Edge: `Summarize this page and include the three most useful links.`
- For the local fixtures, set `MOCK_FORM_ENABLED=1` in `.env` and restart. This is for development only.
- Optional Gmail briefing: install `requirements-gmail.txt`, complete [GMAIL_SETUP.md](GMAIL_SETUP.md), then send `/inbox`.

Consequential actions are approval-gated. Tora stops at CAPTCHA or human-verification pages and asks you to complete them. It does not bypass bot checks, send passwords to the model, or automatically submit a form after a denial.

## What is included

`agent.py`, `tools_browser.py`, `tools_fs.py`, `tools_email.py`, `bridge.py`, `server.py`, `state.py`, `edge_extension/`, `scripts/setup.py`, and the tests are the source of the local agent. `website/dist/` is the public product site; it does not connect to your loopback service or contain credentials.
