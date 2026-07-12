"""JerryAI entry point.

    python main.py                     # Telegram bot mode (needs TELEGRAM_* in .env)
    python main.py --cli "task"        # one-shot CLI task, y/N approvals in console
    python main.py --cli-yes "task"    # one-shot CLI task, auto-approve (careful!)
    python main.py --status            # show per-platform readiness
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from agent import Agent, ToolRegistry
from config import Config, load_config, load_profile, platform_status
from gate import Gate
from job_store import JobStore
from notify import AutoApproveNotifier, CLINotifier, ToolContext
from state import CooldownStore, EventLog, SearchCache


def build_deps(cfg: Config, notifier) -> tuple[Agent, "ToolContextFactory"]:
    log = EventLog(cfg.state_dir / "events.jsonl")
    cooldowns = CooldownStore(cfg.state_dir / "cooldowns.json")
    cooldowns.configure("instagram", cfg.instagram_actions_per_hour,
                        cfg.instagram_min_delay_s[0])
    cache = SearchCache()

    from social import build_adapters
    adapters = build_adapters(cfg, cooldowns)
    job_store = JobStore(cfg.state_dir / "jobs.json")

    registry = ToolRegistry()
    import tools_fs
    tools_fs.register(registry)
    try:
        import tools_browser
        tools_browser.register(registry)
    except ImportError:
        pass  # playwright not installed yet
    import tools_social
    tools_social.register(registry)
    import report_social
    report_social.register(registry)
    import tools_jobs
    tools_jobs.register(registry)

    gate = Gate(approver=notifier, log=log, timeout_s=cfg.approval_timeout_s)
    agent = Agent(cfg, registry, gate, log, profile=load_profile())

    def make_ctx(task_text: str) -> tuple[ToolContext, str]:
        """Returns (ctx, cleaned_task). Leading '!' pre-authorizes writes."""
        pre = task_text.startswith("!")
        cleaned = task_text[1:].lstrip() if pre else task_text
        ctx = ToolContext(
            cfg=cfg, log=log, notifier=notifier, cooldowns=cooldowns,
            cache=cache, adapters=adapters,
            task_id=uuid.uuid4().hex[:8], pre_authorized=pre,
            job_store=job_store, gate=gate,
        )
        return ctx, cleaned

    return agent, make_ctx


ToolContextFactory = object  # typing alias for readability above


async def run_cli(task: str, auto_approve: bool) -> None:
    cfg = load_config()
    notifier = AutoApproveNotifier() if auto_approve else CLINotifier()
    agent, make_ctx = build_deps(cfg, notifier)
    ctx, cleaned = make_ctx(task)
    if ctx.pre_authorized:
        print("[writes pre-authorized for this task]")
    answer = await agent.run_task(cleaned, ctx)
    print("\n--- ANSWER ---")
    print(answer)


def show_status() -> None:
    cfg = load_config()
    print(f"Ollama model: {cfg.ollama_model} @ {cfg.ollama_host}")
    tg = "ready" if (cfg.telegram_bot_token and cfg.telegram_owner_id) else \
         "missing TELEGRAM_BOT_TOKEN / TELEGRAM_OWNER_ID"
    print(f"Telegram: {tg}")
    for platform, (ready, reason) in platform_status(cfg).items():
        mark = "+" if ready else "-"
        print(f"  [{mark}] {platform}: {reason}")


def main() -> None:
    parser = argparse.ArgumentParser(description="JerryAI pocket agent")
    parser.add_argument("--cli", metavar="TASK", help="run one task from the console")
    parser.add_argument("--cli-yes", metavar="TASK",
                        help="run one task, auto-approving gated actions")
    parser.add_argument("--status", action="store_true", help="show platform readiness")
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.cli:
        asyncio.run(run_cli(args.cli, auto_approve=False))
    elif args.cli_yes:
        asyncio.run(run_cli(args.cli_yes, auto_approve=True))
    else:
        cfg = load_config()
        if not (cfg.telegram_bot_token and cfg.telegram_owner_id):
            raise SystemExit(
                "Telegram mode needs TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_ID in .env.\n"
                "Use --cli \"task\" to run without Telegram, or --status to check setup."
            )
        from bridge import run_bridge
        run_bridge(cfg, build_deps)


if __name__ == "__main__":
    main()
