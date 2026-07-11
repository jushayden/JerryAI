"""One-time Instagram login.

Opens a headed Chromium with the persistent profile the agent will reuse.
Log in manually (2FA works naturally), then press Enter here. The session
cookies live in sessions/instagram/ and survive restarts.

Reminder: automating a personal Instagram account violates Instagram's ToS.
JerryAI keeps usage read-mostly and throttled, but the risk is yours.
"""

from __future__ import annotations

import sys
import time

from playwright.sync_api import sync_playwright

from config import load_config


def main() -> None:
    cfg = load_config()
    session_dir = cfg.sessions_dir / "instagram"
    session_dir.mkdir(parents=True, exist_ok=True)

    print("Opening Instagram in a browser window...")
    print("Log in with your account (2FA is fine), then come back here.\n")

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(session_dir), headless=False,
            viewport={"width": 1280, "height": 800})
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.instagram.com/", timeout=60_000)

        input("Press Enter AFTER you are fully logged in (feed visible)... ")

        page.goto("https://www.instagram.com/", timeout=60_000)
        page.wait_for_timeout(3000)
        url = page.url
        if "/accounts/login" in url or "/challenge/" in url:
            print("\nStill on the login/challenge page — session NOT saved.")
            print("Run this script again and complete the login first.")
            context.close()
            sys.exit(1)

        stamp = session_dir / ".login_ok"
        stamp.write_text(f"logged in at {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
                         encoding="utf-8")
        context.close()

    print("\nSession saved. The agent can now read Instagram.")
    print("If Instagram ever challenges the session, run this script again.")
    print("Note: keep usage light — the agent throttles itself to protect your account.")


if __name__ == "__main__":
    main()
