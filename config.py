"""Configuration loading for JerryAI.

All platform keys are optional — adapters degrade gracefully when creds are
missing. Only Telegram bot mode has hard requirements (token + owner id),
enforced in main.py, not here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent


def _opt(name: str) -> str | None:
    """Env var as a non-empty string, else None."""
    val = os.environ.get(name, "").strip()
    return val or None


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str | None
    telegram_owner_id: int | None
    ollama_model: str
    ollama_host: str
    ollama_num_ctx: int
    x_api_key: str | None
    x_api_secret: str | None
    x_access_token: str | None
    x_access_token_secret: str | None
    reddit_client_id: str | None
    reddit_client_secret: str | None
    reddit_user_agent: str | None
    reddit_username: str | None
    reddit_password: str | None
    youtube_api_key: str | None
    project_root: Path
    workspace_dir: Path
    sessions_dir: Path
    downloads_dir: Path
    state_dir: Path
    approval_timeout_s: int = 300
    max_agent_iterations: int = 12
    instagram_actions_per_hour: int = 30
    instagram_min_delay_s: tuple[float, float] = (4.0, 9.0)


def load_config(env_file: str | Path | None = None, root: Path | None = None) -> Config:
    root = (root or PROJECT_ROOT).resolve()
    load_dotenv(env_file or root / ".env")

    owner_raw = _opt("TELEGRAM_OWNER_ID")
    try:
        owner_id = int(owner_raw) if owner_raw else None
    except ValueError:
        owner_id = None

    try:
        num_ctx = int(_opt("OLLAMA_NUM_CTX") or 8192)
    except ValueError:
        num_ctx = 8192

    cfg = Config(
        telegram_bot_token=_opt("TELEGRAM_BOT_TOKEN"),
        telegram_owner_id=owner_id,
        ollama_model=_opt("OLLAMA_MODEL") or "qwen2.5",
        ollama_host=_opt("OLLAMA_HOST") or "http://localhost:11434",
        ollama_num_ctx=num_ctx,
        x_api_key=_opt("X_API_KEY"),
        x_api_secret=_opt("X_API_SECRET"),
        x_access_token=_opt("X_ACCESS_TOKEN"),
        x_access_token_secret=_opt("X_ACCESS_TOKEN_SECRET"),
        reddit_client_id=_opt("REDDIT_CLIENT_ID"),
        reddit_client_secret=_opt("REDDIT_CLIENT_SECRET"),
        reddit_user_agent=_opt("REDDIT_USER_AGENT"),
        reddit_username=_opt("REDDIT_USERNAME"),
        reddit_password=_opt("REDDIT_PASSWORD"),
        youtube_api_key=_opt("YOUTUBE_API_KEY"),
        project_root=root,
        workspace_dir=root / "workspace",
        sessions_dir=root / "sessions",
        downloads_dir=root / "downloads",
        state_dir=root / "state",
    )
    for d in (cfg.workspace_dir, cfg.sessions_dir, cfg.downloads_dir, cfg.state_dir):
        d.mkdir(parents=True, exist_ok=True)
    return cfg


def load_profile(path: Path | None = None) -> dict:
    """Owner profile (handles, interests, persona). {} if missing/invalid."""
    path = path or PROJECT_ROOT / "profile.yaml"
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def platform_status(cfg: Config) -> dict[str, tuple[bool, str]]:
    """Per-platform readiness: platform -> (ready, reason)."""
    status: dict[str, tuple[bool, str]] = {}

    x_keys = (cfg.x_api_key, cfg.x_api_secret, cfg.x_access_token, cfg.x_access_token_secret)
    status["x"] = (
        (True, "ready (free tier: post + profile only, no search)")
        if all(x_keys)
        else (False, "X needs X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET — see SOCIAL_SETUP.md")
    )

    reddit_keys = (cfg.reddit_client_id, cfg.reddit_client_secret, cfg.reddit_user_agent)
    status["reddit"] = (
        (True, "ready")
        if all(reddit_keys)
        else (False, "Reddit needs REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT — see SOCIAL_SETUP.md")
    )

    status["youtube"] = (
        (True, "ready")
        if cfg.youtube_api_key
        else (False, "YouTube needs YOUTUBE_API_KEY — see SOCIAL_SETUP.md")
    )

    ig_stamp = cfg.sessions_dir / "instagram" / ".login_ok"
    status["instagram"] = (
        (True, "ready (browser session)")
        if ig_stamp.exists()
        else (False, "Instagram session not set up — run: python setup_instagram.py")
    )

    return status
