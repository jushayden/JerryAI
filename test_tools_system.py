"""Offline tests for tools_system.py — stubs the hardware helpers, no COM/audio/display.
Run: python test_tools_system.py"""
import asyncio
import inspect

import tools_system


async def deny(summary):
    return False


async def allow(summary):
    return True


async def boom(summary):
    raise AssertionError("confirm_cb must not be called here")


async def main():
    calls = {}

    def fake_volume(level):
        calls["volume"] = level
        return f"vol {level}"

    def fake_mute(on):
        calls["mute"] = on
        return "mute"

    def fake_brightness(level):
        calls["brightness"] = level
        return f"br {level}"

    def fake_lock():
        calls["lock"] = True
        return "Locked."

    def fake_media(action):
        calls["media"] = action
        return f"media {action}"

    def fake_power(action, delay):
        calls["power"] = (action, delay)
        return f"power {action}"

    tools_system._set_volume_sync = fake_volume
    tools_system._set_mute_sync = fake_mute
    tools_system._set_brightness_sync = fake_brightness
    tools_system._lock_sync = fake_lock
    tools_system._media_sync = fake_media
    tools_system._power_sync = fake_power

    # --- registry contract shape ---
    expected = {"set_volume", "mute", "set_brightness", "lock_pc",
                "media_control", "power_action", "system_status"}
    assert set(tools_system.TOOLS) == expected, set(tools_system.TOOLS)
    for name, entry in tools_system.TOOLS.items():
        fn = entry["schema"]["function"]
        assert entry["schema"]["type"] == "function"
        assert fn["name"] == name and fn["description"] and "parameters" in fn
        assert inspect.iscoroutinefunction(entry["fn"]), name
    print("PASS TOOLS registry contract shape (7 tools)")

    # --- volume: valid, clamp, bad input ---
    assert await tools_system.set_volume({"level": 30}) == "vol 30"
    assert calls["volume"] == 30
    await tools_system.set_volume({"level": 150})
    assert calls["volume"] == 100, calls["volume"]  # clamped
    await tools_system.set_volume({"level": -5})
    assert calls["volume"] == 0, calls["volume"]    # clamped
    assert (await tools_system.set_volume({"level": "loud"})).startswith("Error")
    assert (await tools_system.set_volume({})).startswith("Error")
    print("PASS set_volume (clamps 0-100, rejects non-int)")

    # --- brightness clamps too ---
    await tools_system.set_brightness({"level": 200})
    assert calls["brightness"] == 100
    print("PASS set_brightness clamps")

    # --- lock + media pass through, ungated ---
    tools_system.configure(confirm=boom)  # boom = fail if a gate fires
    assert (await tools_system.lock_pc({})) == "Locked." and calls["lock"] is True
    assert (await tools_system.media_control({"action": "playpause"})) == "media playpause"
    assert calls["media"] == "playpause"
    print("PASS lock + media are ungated")

    # --- power_action: invalid action ---
    tools_system.configure(confirm=allow)
    assert (await tools_system.power_action({"action": "explode"})).startswith("Error")
    assert "power" not in calls
    print("PASS power_action rejects unknown action")

    # --- power_action: gated (deny blocks, approve runs) ---
    tools_system.configure(confirm=deny)
    r = await tools_system.power_action({"action": "shutdown"})
    assert r.startswith("User DENIED") and "power" not in calls, r
    tools_system.configure(confirm=allow)
    r = await tools_system.power_action({"action": "shutdown", "delay": 5})
    assert r == "power shutdown" and calls["power"] == ("shutdown", 5), (r, calls.get("power"))
    print("PASS power_action gated (deny blocks, approve runs w/ delay)")

    # --- power_action: sleep is gated too (approve runs) ---
    calls.clear()
    tools_system.configure(confirm=allow)
    assert (await tools_system.power_action({"action": "sleep"})) == "power sleep"
    assert calls["power"] == ("sleep", 0)
    print("PASS power_action sleep gated (approve runs)")

    # --- cancel is ungated even mid-gate ---
    calls.clear()
    tools_system.configure(confirm=boom)
    assert (await tools_system.power_action({"action": "cancel"})) == "power cancel"
    assert calls["power"] == ("cancel", 0)
    print("PASS power_action cancel is ungated")

    tools_system.configure()  # reset
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
