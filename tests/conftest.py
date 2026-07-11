import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config  # noqa: E402
from notify import AutoApproveNotifier, ToolContext  # noqa: E402
from state import CooldownStore, EventLog, SearchCache  # noqa: E402

CLEAR_VARS = [
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID", "OLLAMA_MODEL", "OLLAMA_HOST",
    "OLLAMA_NUM_CTX", "X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN",
    "X_ACCESS_TOKEN_SECRET", "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET",
    "REDDIT_USER_AGENT", "REDDIT_USERNAME", "REDDIT_PASSWORD", "YOUTUBE_API_KEY",
]


@pytest.fixture
def clean_env(monkeypatch):
    for var in CLEAR_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def tmp_config(tmp_path, clean_env):
    # env_file that doesn't exist -> nothing loaded from the real .env
    return load_config(env_file=tmp_path / "nonexistent.env", root=tmp_path)


@pytest.fixture
def fake_clock():
    class Clock:
        now = 1_000_000.0

        def __call__(self):
            return self.now

        def advance(self, s):
            self.now += s

    return Clock()


@pytest.fixture
def tool_ctx(tmp_config):
    return ToolContext(
        cfg=tmp_config,
        log=EventLog(tmp_config.state_dir / "events.jsonl"),
        notifier=AutoApproveNotifier(),
        cooldowns=CooldownStore(tmp_config.state_dir / "cooldowns.json"),
        cache=SearchCache(),
        adapters={},
        task_id="test-task",
    )
