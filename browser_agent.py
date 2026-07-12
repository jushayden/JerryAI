"""browser-use integration for job applications.

Uses browser-use's own Agent (DOM-reasoning; vision is disabled — see
build_llm) instead of selector-guessing, specifically for multi-step job
application forms. Local Ollama backend by default, consistent with the
rest of this project.

This module is independent of tools_browser.py's Playwright/BROWSER_EXECUTOR
stack — browser-use manages its own separate browser process via its own
CDP-based harness (browser-harness/cdp-use), not Playwright, so there is no
shared-thread contention with the generic browser tools or the Instagram
adapter.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from job_profile import ApplicantProfile, domain_of, load_site_credentials

DEFAULT_MAX_STEPS = 40


@dataclass
class ApplyResult:
    status: str  # "submitted" | "denied" | "failed" | "dry_run" | "exploring"
    summary: str
    screenshot_path: str | None = None


def build_llm(cfg):
    from browser_use import ChatOllama
    # use_vision must be False wherever this LLM is used (see run_apply_agent) —
    # qwen2.5 (and most local Ollama text models) reject multimodal requests
    # outright with a 400, which otherwise makes every single step fail and
    # silently degrades into random unrelated exploration.
    return ChatOllama(model=cfg.ollama_model, host=cfg.ollama_host)


def _resolve_workspace_file(cfg, rel_or_abs: str) -> str | None:
    if not rel_or_abs:
        return None
    p = Path(rel_or_abs)
    if not p.is_absolute():
        p = cfg.workspace_dir / p
    return str(p) if p.is_file() else None


def _build_task_text(url: str, profile: ApplicantProfile, has_resume: bool,
                     has_cover_letter: bool, has_creds: bool,
                     extra_instructions: str) -> str:
    lines = [
        f"Go to {url} and fill out the job application form using the applicant's "
        f"details below. Stay on this application — do not wander to unrelated pages.",
        "",
        f"Applicant: {profile.first_name} {profile.last_name}, email {profile.email}, "
        f"phone {profile.phone}.",
    ]
    if profile.linkedin:
        lines.append(f"LinkedIn: {profile.linkedin}")
    addr = profile.address or {}
    if addr:
        lines.append(
            f"Address: {addr.get('line1', '')}, {addr.get('city', '')}, "
            f"{addr.get('postal_code', '')}, {addr.get('country', '')}"
        )
    wa = profile.work_authorization or {}
    if wa:
        lines.append(
            f"Work authorization: US citizen = {wa.get('us_citizen', False)}, "
            f"sponsorship needed = {wa.get('sponsorship_needed', False)}"
        )
    if has_resume:
        lines.append("A resume file is available in available_file_paths — attach it "
                     "to any resume/CV upload field.")
    if has_cover_letter:
        lines.append("A cover letter file is available in available_file_paths — attach "
                     "it to any cover letter upload field.")
    if profile.custom_qa:
        lines.append("If the form asks any of these exact questions, use these answers:")
        for q, a in profile.custom_qa.items():
            lines.append(f'  Q: "{q}" -> A: "{a}"')
    if has_creds:
        lines.append(
            "If the site requires logging in or creating an account first, use the "
            "secure credential values provided separately for this domain (referenced "
            "by their field name) — never invent credentials, never ask the owner for "
            "a password directly."
        )
    lines.append(
        "Dismiss any cookie-consent banner or modal first if one blocks the form. "
        "Fill every field the form actually has — never invent a field that isn't "
        "really there. When the form is completely and correctly filled and ready to "
        "send, call click_final_submit with the index of the submit/apply/send button "
        "— do NOT use the normal click action on it yourself. If you hit a CAPTCHA or "
        "a 'verify you are human' challenge, stop immediately and call done reporting "
        "that a human needs to complete it — never try to solve or bypass one."
    )
    if extra_instructions:
        lines.append(extra_instructions)
    return "\n".join(lines)


def build_apply_agent_tools(ctx, gate, job_url: str, dry_run: bool, profile: ApplicantProfile):
    """Fresh Tools instance per apply_to_job call — closes over ctx/gate/job_url/profile
    rather than being a module-level singleton, so concurrent applications (were they
    ever to happen) wouldn't share state."""
    from browser_use import ActionResult, Tools
    from browser_use.browser.events import ClickElementEvent

    tools = Tools()

    @tools.action(description=(
        "Final step only. Call this instead of clicking any Submit/Apply/Send button "
        "yourself, once the form is completely and correctly filled. Pass the index of "
        "the submit button you identified."
    ))
    async def click_final_submit(index: int, browser_session) -> "ActionResult":
        screenshot_path: str | None = None
        try:
            shot = ctx.cfg.downloads_dir / f"apply_{int(time.time())}.png"
            shot.parent.mkdir(parents=True, exist_ok=True)
            await browser_session.take_screenshot(path=str(shot))
            screenshot_path = str(shot)
        except Exception:
            pass  # screenshot is best-effort — never block the flow on it

        if dry_run:
            return ActionResult(
                extracted_content=("DRY RUN: form is ready to submit. No approval "
                                   "requested, no click performed."),
                is_done=True,
            )

        payload = (
            f"Job application ready to submit\n"
            f"URL: {job_url}\n"
            f"Applicant: {profile.first_name} {profile.last_name} <{profile.email}>"
        )
        if screenshot_path:
            try:
                await ctx.notifier.send_photos([Path(screenshot_path)],
                                               caption="Ready to submit — review before approving")
            except Exception:
                pass

        approved = await gate.check_custom(
            action_label="apply_to_job.submit",
            payload_text=payload,
            ctx=ctx,
            timeout_s=ctx.cfg.approval_timeout_s,
        )
        if not approved:
            return ActionResult(
                extracted_content=("DENIED by owner — do not retry. Report this back "
                                   "as your final answer."),
                is_done=True,
            )

        node = await browser_session.get_element_by_index(index)
        if node is None:
            return ActionResult(
                error=f"Submit element index {index} not available — page may have changed.")
        event = browser_session.event_bus.dispatch(ClickElementEvent(node=node))
        await event
        return ActionResult(extracted_content="Application submitted.", is_done=True)

    return tools


async def run_apply_agent(ctx, gate, url: str, profile_dict: dict,
                          extra_instructions: str = "", dry_run: bool = False,
                          max_steps: int = DEFAULT_MAX_STEPS) -> ApplyResult:
    from browser_use import Agent

    profile = ApplicantProfile.from_profile_dict(profile_dict)
    domain = domain_of(url)
    site_creds = load_site_credentials(profile_dict, domain)
    sensitive_data = {domain: site_creds} if site_creds else None

    resume_path = _resolve_workspace_file(ctx.cfg, profile.resume_path)
    cover_letter_path = _resolve_workspace_file(ctx.cfg, profile.cover_letter_path)
    available_file_paths = [p for p in (resume_path, cover_letter_path) if p]

    task = _build_task_text(
        url, profile,
        has_resume=bool(resume_path), has_cover_letter=bool(cover_letter_path),
        has_creds=bool(site_creds), extra_instructions=extra_instructions,
    )

    llm = build_llm(ctx.cfg)
    tools = build_apply_agent_tools(ctx, gate, url, dry_run, profile)
    agent = Agent(
        task=task,
        llm=llm,
        tools=tools,
        sensitive_data=sensitive_data,
        available_file_paths=available_file_paths or None,
        use_vision=False,  # see build_llm — required for non-vision local models
    )
    history = await agent.run(max_steps=max_steps)

    summary = str(history.final_result() or "(no final result returned)")
    shots = history.screenshot_paths() if hasattr(history, "screenshot_paths") else None
    shot = shots[-1] if shots else None

    if "DENIED by owner" in summary:
        status = "denied"
    elif dry_run and "DRY RUN" in summary:
        status = "dry_run"
    elif "Application submitted" in summary or "submitted" in summary.lower():
        status = "submitted"
    elif history.has_errors():
        status = "failed"
    else:
        status = "exploring"
    return ApplyResult(status=status, summary=summary, screenshot_path=shot)
