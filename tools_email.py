"""Gmail tools for Pocket Agent: read the inbox, send/reply/draft, and tidy up
(mark read, archive, trash). One-time OAuth setup: see GMAIL_SETUP.md.

Safety: sending, replying, and trashing are IRREVERSIBLE-ish, so they route through
the same phone-approval flow as file deletion — the tool calls confirm_cb (set via
configure()) and only proceeds on Approve, unless the task was `!`-preauthorized.
Drafting, marking read, and archiving are reversible and run without a gate.
"""
import asyncio
import base64
import html
import json
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

import config

# gmail.modify covers read + send + drafts + trash. It cannot PERMANENTLY delete —
# trashed mail is recoverable, which is the point.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
CREDS_PATH = config.PROJECT_ROOT / "credentials.json"
TOKEN_PATH = config.PROJECT_ROOT / "token.json"

DIGEST_MAX = 3500   # chars for the whole scan_inbox digest
SNIPPET_MAX = 100   # chars per message snippet
SUBJECT_MAX = 100   # chars per subject line
BODY_PREVIEW_MAX = 400  # chars of body shown on the approval card

_service_cache = None


# --- confirmation state (set via configure(), mirrors tools_fs) ---

async def _default_confirm(summary: str) -> bool:
    ans = await asyncio.to_thread(input, f"{summary}\nApprove? y/n: ")
    return ans.strip().lower() in ("y", "yes")

confirm_cb = _default_confirm


def configure(confirm=None):
    """Set the confirmation callback used to gate send/reply/trash (mirrors tools_fs).

    Call this per task (as main._run_real_agent does) so send/reply/trash prompt the
    user's phone rather than a blocking console input.
    """
    global confirm_cb
    confirm_cb = confirm if confirm is not None else _default_confirm


def _service():
    """Return a cached, authenticated Gmail service.

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
        # Discard a token granted NARROWER scopes than we now need (e.g. an old read-only
        # token from before send was added) so we re-consent once here, instead of the
        # first send failing later with a 403 insufficient-permission error.
        try:
            granted = set(json.loads(TOKEN_PATH.read_text(encoding="utf-8")).get("scopes", []))
        except Exception:
            granted = set()
        if set(SCOPES).issubset(granted):
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        else:
            TOKEN_PATH.unlink(missing_ok=True)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None  # dead refresh token (e.g. Testing-mode 7-day expiry) -> re-consent
        if not creds or not creds.valid:
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


def _confirm_text(action: str, to: str, subject: str, body: str,
                  cc: str | None = None, bcc: str | None = None) -> str:
    """Build the approval-card body shown on the phone before a send."""
    lines = [action, f"To: {to}"]
    if cc:
        lines.append(f"Cc: {cc}")
    if bcc:
        lines.append(f"Bcc: {bcc}")
    lines.append(f"Subject: {subject or '(no subject)'}")
    lines.append("")
    preview = body if len(body) <= BODY_PREVIEW_MAX else body[:BODY_PREVIEW_MAX] + "…"
    lines.append(preview or "(empty body)")
    return "\n".join(lines)


# --- blocking Gmail calls (run in a worker thread via asyncio.to_thread) ---

def _scan_sync(hours: int, max_results: int) -> str:
    """Blocking Gmail scan; returns a compact inbox digest."""
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


def _find_sync(query: str) -> dict | None:
    """Return metadata for the newest message matching a Gmail search query, or None."""
    svc = _service()
    resp = svc.users().messages().list(userId="me", q=query, maxResults=1).execute()
    msgs = resp.get("messages", [])
    if not msgs:
        return None
    mid = msgs[0]["id"]
    msg = svc.users().messages().get(
        userId="me", id=mid, format="metadata",
        metadataHeaders=["From", "Subject", "Message-ID", "References"],
    ).execute()
    hdrs = {h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])}
    return {
        "id": mid,
        "threadId": msg.get("threadId"),
        "from": hdrs.get("from", ""),
        "subject": hdrs.get("subject", "(no subject)"),
        "message_id": hdrs.get("message-id", ""),
        "references": hdrs.get("references", ""),
        "snippet": html.unescape(msg.get("snippet", "")),
    }


def _build_raw(to: str, subject: str, body: str, cc: str | None = None,
               bcc: str | None = None, headers: dict | None = None) -> str:
    """Build a base64url-encoded RFC 822 message for the Gmail send/draft API."""
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    msg["Subject"] = subject
    for k, v in (headers or {}).items():
        if v:
            msg[k] = v
    msg.set_content(body or "")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def _send_sync(to: str, subject: str, body: str, cc: str | None, bcc: str | None) -> str:
    svc = _service()
    raw = _build_raw(to, subject, body, cc, bcc)
    sent = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    return f"Sent to {to} (Gmail id {sent.get('id')})."


def _reply_sync(match: dict, body: str) -> str:
    svc = _service()
    subject = match["subject"]
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    refs = (match.get("references", "") + " " + match.get("message_id", "")).strip()
    raw = _build_raw(match["from"], subject, body, headers={
        "In-Reply-To": match.get("message_id", ""),
        "References": refs,
    })
    sent = svc.users().messages().send(
        userId="me", body={"raw": raw, "threadId": match["threadId"]}).execute()
    return f"Reply sent to {match['from']} in the same thread (Gmail id {sent.get('id')})."


def _draft_sync(to: str, subject: str, body: str, cc: str | None, bcc: str | None) -> str:
    svc = _service()
    raw = _build_raw(to, subject, body, cc, bcc)
    draft = svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return f"Draft saved to {to} (draft id {draft.get('id')}). Nothing was sent."


def _trash_sync(mid: str) -> str:
    svc = _service()
    svc.users().messages().trash(userId="me", id=mid).execute()
    return "Moved to Trash (recoverable from Gmail's Trash for 30 days)."


# --- tool functions (never raise; return "Error: ..." strings) ---

async def scan_inbox(args: dict) -> str:
    """Read the Gmail inbox and return a compact digest of recent messages."""
    try:
        hours = int(args.get("hours") or 24)
        max_results = int(args.get("max_results") or 30)
        return await asyncio.to_thread(_scan_sync, hours, max_results)
    except Exception as e:
        return f"Error: {e}"


async def send_email(args: dict) -> str:
    """Send a brand-new email. Gated: the user approves on their phone first."""
    try:
        to = str(args.get("to") or "").strip()
        subject = str(args.get("subject") or "").strip()
        body = str(args.get("body") or "")
        cc = str(args.get("cc") or "").strip() or None
        bcc = str(args.get("bcc") or "").strip() or None
        if not to:
            return "Error: send_email needs a 'to' address."
        ok = await confirm_cb(_confirm_text("Send a new email", to, subject, body, cc, bcc))
        if not ok:
            return "User DENIED sending this email. Do not retry — report that it was not sent."
        return await asyncio.to_thread(_send_sync, to, subject, body, cc, bcc)
    except Exception as e:
        return f"Error: {e}"


async def reply_email(args: dict) -> str:
    """Reply in-thread to the newest message matching a Gmail search query. Gated."""
    try:
        query = str(args.get("query") or "").strip()
        body = str(args.get("body") or "")
        if not query:
            return ("Error: reply_email needs a 'query' identifying the message to reply to "
                    "(Gmail search syntax, e.g. 'from:alice@x.com subject:invoice').")
        match = await asyncio.to_thread(_find_sync, query)
        if match is None:
            return (f"No message matched '{query}'. Run scan_inbox to see what's there, "
                    "or refine the query.")
        subj = match["subject"] if match["subject"].lower().startswith("re:") else "Re: " + match["subject"]
        ok = await confirm_cb(_confirm_text("Reply to an email", match["from"], subj, body))
        if not ok:
            return "User DENIED sending this reply. Do not retry."
        return await asyncio.to_thread(_reply_sync, match, body)
    except Exception as e:
        return f"Error: {e}"


async def create_draft(args: dict) -> str:
    """Save an email as a draft without sending it (reversible, so no approval needed)."""
    try:
        to = str(args.get("to") or "").strip()
        subject = str(args.get("subject") or "").strip()
        body = str(args.get("body") or "")
        cc = str(args.get("cc") or "").strip() or None
        bcc = str(args.get("bcc") or "").strip() or None
        if not to:
            return "Error: create_draft needs a 'to' address."
        return await asyncio.to_thread(_draft_sync, to, subject, body, cc, bcc)
    except Exception as e:
        return f"Error: {e}"


async def trash_email(args: dict) -> str:
    """Move the newest message matching a query to Trash. Gated (recoverable delete)."""
    try:
        query = str(args.get("query") or "").strip()
        if not query:
            return "Error: trash_email needs a 'query' identifying the message to trash."
        match = await asyncio.to_thread(_find_sync, query)
        if match is None:
            return f"No message matched '{query}'."
        summary = (f"Move this email to Trash:\nFrom: {match['from']}\n"
                   f"Subject: {match['subject']}\n\n\"{_one_line(match['snippet'], 200)}\"")
        ok = await confirm_cb(summary)
        if not ok:
            return "User DENIED trashing this email. Do not retry."
        return await asyncio.to_thread(_trash_sync, match["id"])
    except Exception as e:
        return f"Error: {e}"


def _schema(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        },
    }


_TO = {"type": "string", "description": "Recipient email address (comma-separate several)"}
_SUBJECT = {"type": "string", "description": "Subject line"}
_BODY = {"type": "string", "description": "Plain-text body of the email"}
_CC = {"type": "string", "description": "Optional Cc address(es)"}
_BCC = {"type": "string", "description": "Optional Bcc address(es)"}
_QUERY = {"type": "string",
          "description": "Gmail search to locate the target message (newest match wins), "
                         "e.g. 'from:alice@x.com subject:invoice' or 'is:unread newer_than:2d'"}


TOOLS: dict[str, dict] = {
    "scan_inbox": {
        "schema": _schema(
            "scan_inbox",
            "Read the user's Gmail inbox and return recent messages: sender, subject, "
            "unread status, snippet. Use for inbox briefings.",
            {"hours": {"type": "integer", "description": "Look-back window in hours, default 24"}},
            [],
        ),
        "fn": scan_inbox,
    },
    "send_email": {
        "schema": _schema(
            "send_email",
            "Send a NEW email from the user's Gmail. The system automatically shows the user "
            "an approval card on their phone with the full recipient/subject/body before it "
            "sends — do NOT ask the user for permission yourself, just call this. Use "
            "read_profile if you need the user's own details; never invent addresses.",
            {"to": _TO, "subject": _SUBJECT, "body": _BODY, "cc": _CC, "bcc": _BCC},
            ["to", "subject", "body"],
        ),
        "fn": send_email,
    },
    "reply_email": {
        "schema": _schema(
            "reply_email",
            "Reply in-thread to an existing email. Find it with a Gmail search 'query'; the "
            "reply goes to the original sender in the same thread. Approval is requested on "
            "the user's phone automatically before it sends.",
            {"query": _QUERY, "body": _BODY},
            ["query", "body"],
        ),
        "fn": reply_email,
    },
    "create_draft": {
        "schema": _schema(
            "create_draft",
            "Save an email as a Gmail draft WITHOUT sending it. Reversible, so no approval is "
            "needed. Use when the user wants to review/finish it later, or when you're unsure.",
            {"to": _TO, "subject": _SUBJECT, "body": _BODY, "cc": _CC, "bcc": _BCC},
            ["to", "subject", "body"],
        ),
        "fn": create_draft,
    },
    "trash_email": {
        "schema": _schema(
            "trash_email",
            "Move an email (newest match of a Gmail search 'query') to Trash. Recoverable for "
            "30 days. Approval is requested on the user's phone automatically.",
            {"query": _QUERY},
            ["query"],
        ),
        "fn": trash_email,
    },
}
