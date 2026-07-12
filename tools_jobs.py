"""Job-application tool — wraps browser_agent.run_apply_agent with the
applied-jobs dedup store and this project's standard tool contract.

apply_to_job itself is NOT in gate.GATED_TOOLS — the true approval checkpoint
is the click_final_submit callback deep inside browser_agent's own run, which
fires unconditionally on every real submit attempt (see gate.Gate.check_custom).
"""

from __future__ import annotations

from config import load_profile
from notify import ToolContext


async def apply_to_job(ctx: ToolContext, url: str, extra_instructions: str = "") -> str:
    if ctx.job_store is None or ctx.gate is None:
        return "ERROR: apply_to_job is not wired up (job_store/gate missing from context)."

    if ctx.job_store.has_applied(url):
        rec = ctx.job_store.get(url)
        return f"SKIPPED — already applied to {url} on {rec.applied_at}."
    if ctx.job_store.is_in_progress(url):
        return f"SKIPPED — an application to {url} is already in progress."

    profile_dict = load_profile()
    from job_profile import ApplicantProfile
    profile = ApplicantProfile.from_profile_dict(profile_dict)
    if not profile.is_configured():
        return ("ERROR: no applicant profile configured. Add an `applicant:` block "
                "with at least first_name and email to profile.yaml before applying to jobs.")

    ctx.job_store.mark_in_progress(url)
    try:
        from browser_agent import run_apply_agent
        result = await run_apply_agent(
            ctx, ctx.gate, url, profile_dict,
            extra_instructions=extra_instructions,
            dry_run=ctx.cfg.browser_use_dry_run,
            max_steps=ctx.cfg.apply_to_job_max_steps,
        )
    except Exception as e:
        ctx.job_store.mark_failed(url, notes=f"{type(e).__name__}: {e}")
        return f"ERROR: {type(e).__name__}: {e}. Not retried automatically."

    if result.status == "submitted":
        ctx.job_store.mark_applied(url, notes=result.summary[:500])
        return f"Applied to {url}.\n{result.summary}"
    if result.status == "denied":
        ctx.job_store.mark_denied(url, notes=result.summary[:500])
        return f"DENIED by owner — application not submitted for {url}."
    if result.status == "dry_run":
        return f"DRY RUN for {url} — form was filled but not submitted.\n{result.summary}"
    ctx.job_store.mark_failed(url, notes=result.summary[:500])
    return f"FAILED to complete the application for {url}.\n{result.summary}"


def _schema(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


def register(registry) -> None:
    from agent import ToolSpec
    registry.register(ToolSpec(
        name="apply_to_job",
        func=apply_to_job,
        gated=False,  # the real gate is the inner click_final_submit callback
        timeout_s=900,  # a multi-step browser-use run + a Telegram approval wait needs far more than the 120s default
        schema=_schema(
            "apply_to_job",
            "Fill out and (after your approval, requested automatically before the final "
            "submit) apply to a job posting at the given URL, using the applicant profile "
            "from profile.yaml. Skips URLs already applied to.",
            {"url": {"type": "string", "description": "The job posting/application URL"},
             "extra_instructions": {"type": "string",
                                    "description": "Any extra guidance for this specific application"}},
            ["url"],
        ),
    ))
