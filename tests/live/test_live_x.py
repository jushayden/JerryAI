"""Live smoke test — needs X creds and RUN_LIVE_TESTS=1.

AUTH CHECK ONLY. This test never posts anything.
"""

import os

import pytest

from config import load_config
from social.x_adapter import XAdapter

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1", reason="RUN_LIVE_TESTS != 1")


def test_x_auth_live():
    adapter = XAdapter(load_config())
    if not adapter.is_available():
        pytest.skip("X creds not configured")
    me = adapter._api().get_me()
    assert me.data is not None and me.data.username
