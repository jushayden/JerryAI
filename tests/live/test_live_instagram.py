"""Live smoke test — needs a saved Instagram session and RUN_LIVE_TESTS=1.

Read-only: fetches one public profile. Counts against the built-in throttle.
Runs the adapter on a pw-named thread to satisfy the browser-thread assertion.
"""

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from config import load_config
from state import CooldownStore

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1", reason="RUN_LIVE_TESTS != 1")


def test_instagram_profile_live():
    from social.instagram_adapter import InstagramAdapter

    cfg = load_config()
    cooldowns = CooldownStore(cfg.state_dir / "cooldowns.json")
    cooldowns.configure("instagram", cfg.instagram_actions_per_hour,
                        cfg.instagram_min_delay_s[0])
    adapter = InstagramAdapter(cfg, cooldowns)
    if not adapter.is_available():
        pytest.skip("Instagram session not set up")
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="pw") as pool:
        profile = pool.submit(adapter.get_profile, "instagram").result(timeout=120)
        pool.submit(adapter._session.close).result(timeout=30)
    assert profile["username"] == "@instagram"
    assert profile["header_text"]
