"""Gmail read-only tools for Tora AI. One-time OAuth setup: see GMAIL_SETUP.md."""
import asyncio
import html
from email.utils import parsedate_to_datetime

import config

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDS_PATH = config.PROJECT_ROOT / "credentials.json"
TOKEN_PATH = config.PROJECT_ROOT / "token.json"

DIGEST_MAX = 3500   # chars for the whole scan_inbox digest
SNIPPET_MAX = 100   # chars per message snippet
SUBJECT_MAX = 100   # chars per subject line

_service_cache = None


def _service():
    """Return a cached, authenticated read-only Gmail service.

    Loads token.json if present, refreshes it if expired, otherwise runs the
    installed-app OAuth flow (opens a browser once for consent — expected on
    first use) and caches the result in token.json.
    """
    global _service_cache
    if _service_cache is not None:
        return _service_cache

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as e:
        raise RuntimeError(
            "Google API libraries missing. Run: "
            "pip install --user google-api-python-client google-auth-oauthlib"
        ) from e

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDS_PATH.exists():
                raise RuntimeError(
                    f"Gmail is not set up: {CREDS_PATH} not found. "
                    "Follow GMAIL_SETUP.md to create an OAuth client and save "
                    "credentials.json, then retry."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)  # opens a browser once for consent
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    _service_cache = build("gmail", "v1", credentials=creds)
    return _service_cache


def _one_line(s: str, limit: int) -> str:
    """Collapse whitespace to a single line and trim to limit chars."""
    s = " ".join(str(s).split())
    return s[:limit] + "…" if len(s) > limit else s


def _scan_sync(hours: int, max_results: int) -> str:
    """Blocking Gmail scan; runs in a worker thread via asyncio.to_thread."""
    svc = _service()
    resp = svc.users().messages().list(
        userId="me", q=f"in:inbox newer_than:{hours}h", maxResults=max_results
    ).execute()
    ids = [m["id"] for m in resp.get("messages", [])]
    if not ids:
        return f"No messages in the last {hours}h."

    lines, unread_total = [], 0
    for i, mid in enumerate(ids, 1):
        msg = svc.users().messages().get(
            userId="me", id=mid, format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        hdrs = {h["name"].lower(): h["value"]
                for h in msg.get("payload", {}).get("headers", [])}
        unread = "UNREAD" in msg.get("labelIds", [])
        unread_total += unread
        try:
            date = parsedate_to_datetime(hdrs.get("date", "")).strftime("%b %d %H:%M")
        except Exception:
            date = _one_line(hdrs.get("date", ""), 22)
        snippet = _one_line(html.unescape(msg.get("snippet", "")), SNIPPET_MAX)
        parts = [
            f'From: {_one_line(hdrs.get("from", "?"), 100)}',
            f'Subj: {_one_line(hdrs.get("subject", "(no subject)"), SUBJECT_MAX)}',
        ]
        if date:
            parts.append(date)
        parts.append(f'"{snippet}"')
        flag = "[UNREAD] " if unread else ""
        lines.append(f"{i}. {flag}" + " | ".join(parts))

    header = f"Inbox, last {hours}h: {len(ids)} messages ({unread_total} unread)"
    out, total = [header], len(header)
    shown = 0
    for line in lines:
        if total + 1 + len(line) > DIGEST_MAX:
            break
        out.append(line)
        total += 1 + len(line)
        shown += 1
    if shown < len(lines):
        out.append(f"…and {len(lines) - shown} more")
    return "\n".join(out)


# --- tool functions (never raise; return "Error: ..." strings) ---

async def scan_inbox(args: dict) -> str:
    """Scan the Gmail inbox for recent messages and return a compact digest."""
    try:
        hours = int(args.get("hours") or 24)
        max_results = int(args.get("max_results") or 30)
        return await asyncio.to_thread(_scan_sync, hours, max_results)
    except Exception as e:
        return f"Error: {e}"


TOOLS: dict[str, dict] = {
    "scan_inbox": {
        "schema": {
            "type": "function",
            "function": {
                "name": "scan_inbox",
                "description": (
                    "Read the user's Gmail inbox (read-only) and return recent "
                    "messages: sender, subject, unread status, snippet. "
                    "Use for inbox briefings."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "hours": {
                            "type": "integer",
                            "description": "Look-back window in hours, default 24",
                        },
                    },
                    "required": [],
                },
            },
        },
        "fn": scan_inbox,
    },
}
