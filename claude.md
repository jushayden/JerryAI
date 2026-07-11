# JerryAI — notes for Claude Code

Personal "pocket agent": Telegram bot → Ollama tool loop → tools (fs, browser,
social). See README.md for usage, SOCIAL_SETUP.md for keys.

## Architecture invariants (do not break)

- **Gating is deterministic**: whether a tool needs owner approval is a table
  lookup in `gate.py` (`GATED_TOOLS`), never a model decision. New write-capable
  tools must be added there and registered with `gated=True`.
- **Single event loop**: everything async runs on python-telegram-bot's loop.
  Agent tasks are spawned with `application.create_task`; approvals suspend on
  `asyncio.Future`s in `bridge.PendingApprovals`, resolved by the callback
  handler. Never block the loop — sync libraries (praw/tweepy/googleapiclient)
  go through `asyncio.to_thread`.
- **Playwright is sync-API on ONE pinned thread** (`tools_browser.BROWSER_EXECUTOR`,
  max_workers=1, thread prefix "pw"). Sync Playwright objects have greenlet
  thread affinity — never touch them via `asyncio.to_thread` or from the loop.
  `BrowserSession._assert_thread` enforces this.
- **Tools are async functions `(ctx: ToolContext, **kwargs) -> str`** and must
  return strings; exceptions are caught by `agent._handle_call` and become
  "ERROR: ..." tool results. Tools talk to the owner only via `ctx.notifier`.
- **Every action is audited** to `state/events.jsonl` via `state.EventLog`
  (fixed kind vocabulary — extend `EVENT_KINDS` when adding kinds).
- **Small-model hardening** lives in `agent.extract_tool_calls` (stringified
  args, JSON-in-content, fenced blocks). Keep tool schemas flat (string/int/
  enum params); nested objects break 7B models.
- **Instagram adapter is read-mostly and throttled** (cooldowns configured in
  `main.build_deps`). Never add retry loops around login challenges.

## Commands

- Tests: `.venv\Scripts\python -m pytest` (offline; live tests need RUN_LIVE_TESTS=1)
- Status: `.venv\Scripts\python main.py --status`
- CLI task: `.venv\Scripts\python main.py --cli "task"`
- Python: 3.12 venv at `.venv` (uv-managed 3.12.13; system 3.14 untested with Playwright)
