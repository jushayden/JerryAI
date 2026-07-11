"""Windows system-control tools for Pocket Agent: volume, brightness, media keys,
lock, and power (sleep/shutdown/restart). Control your PC from your phone.

Safety: power actions (sleep/shutdown/restart) are disruptive, so they route through the
same phone-approval flow as file deletion — power_action calls confirm_cb (set via
configure()) and only proceeds on Approve. There is no preauth bypass (matching the rest
of the project). Volume/brightness/media/lock are harmless and run without a gate.

Windows-only: the hardware calls (ctypes.windll, pycaw, screen_brightness_control) are
imported lazily inside the helpers, so this module still imports on other platforms — the
tools just return a graceful "Error: ..." there.
"""
import asyncio
import subprocess


# --- confirmation state (set via configure(), mirrors tools_fs/tools_email) ---

async def _default_confirm(summary: str) -> bool:
    ans = await asyncio.to_thread(input, f"{summary}\nApprove? y/n: ")
    return ans.strip().lower() in ("y", "yes")

confirm_cb = _default_confirm


def configure(confirm=None):
    """Set the confirmation callback for power actions."""
    global confirm_cb
    confirm_cb = confirm if confirm is not None else _default_confirm


# --- low-level helpers (blocking; run via asyncio.to_thread, stubbable in tests) ---

def _volume_endpoint():
    """Return the Windows master-volume COM interface (pycaw)."""
    try:
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    except ImportError as e:
        raise RuntimeError("pycaw not installed — run: pip install pycaw") from e
    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def _set_volume_sync(level: int) -> str:
    vol = _volume_endpoint()
    vol.SetMasterVolumeLevelScalar(level / 100.0, None)
    if level > 0:
        vol.SetMute(0, None)  # raising volume implies un-mute
    return f"Volume set to {round(vol.GetMasterVolumeLevelScalar() * 100)}%."


def _set_mute_sync(on: bool) -> str:
    _volume_endpoint().SetMute(1 if on else 0, None)
    return "Muted." if on else "Unmuted."


def _set_brightness_sync(level: int) -> str:
    try:
        import screen_brightness_control as sbc
    except ImportError as e:
        raise RuntimeError("screen_brightness_control not installed — "
                           "run: pip install screen-brightness-control") from e
    sbc.set_brightness(level)
    cur = sbc.get_brightness()
    shown = cur[0] if isinstance(cur, list) and cur else level
    return f"Brightness set to {shown}%."


def _lock_sync() -> str:
    import ctypes
    ok = ctypes.windll.user32.LockWorkStation()
    return "Locked." if ok else "Error: LockWorkStation failed."


_MEDIA_VK = {"playpause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}


def _media_sync(action: str) -> str:
    code = _MEDIA_VK.get(action)
    if code is None:
        return "Error: media action must be playpause, next, previous, or stop."
    import ctypes
    user32 = ctypes.windll.user32
    user32.keybd_event(code, 0, 0, 0)  # key down
    user32.keybd_event(code, 0, 2, 0)  # key up (KEYEVENTF_KEYUP)
    return f"Sent media key: {action}."


def _power_sync(action: str, delay: int) -> str:
    if action == "sleep":
        # non-blocking: schedule the suspend and return, so the report sends first
        subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
        return "Sleeping now."
    if action == "cancel":
        subprocess.run(["shutdown", "/a"], capture_output=True)
        return "Cancelled any pending shutdown/restart."
    flag = "/s" if action == "shutdown" else "/r"
    subprocess.run(["shutdown", flag, "/t", str(delay)], capture_output=True)
    verb = "Shutting down" if action == "shutdown" else "Restarting"
    return f"{verb} in {delay}s. Send power_action with action 'cancel' to abort."


def _status_sync() -> str:
    lines = []
    try:
        vol = _volume_endpoint()
        pct = round(vol.GetMasterVolumeLevelScalar() * 100)
        muted = " (muted)" if vol.GetMute() else ""
        lines.append(f"Volume: {pct}%{muted}")
    except Exception as e:
        lines.append(f"Volume: unavailable ({e})")
    try:
        import screen_brightness_control as sbc
        b = sbc.get_brightness()
        lines.append(f"Brightness: {b[0] if isinstance(b, list) and b else b}%")
    except Exception as e:
        lines.append(f"Brightness: unavailable ({e})")
    return "\n".join(lines)


# --- tool functions (never raise; return "Error: ..." strings) ---

def _level(args: dict) -> int:
    """Parse + clamp a 0-100 'level' argument. Raises ValueError if missing/bad."""
    level = int(args.get("level"))
    return max(0, min(100, level))


async def set_volume(args: dict) -> str:
    """Set the system master volume to a level 0-100."""
    try:
        level = _level(args)
    except (TypeError, ValueError):
        return "Error: set_volume needs an integer 'level' 0-100."
    try:
        return await asyncio.to_thread(_set_volume_sync, level)
    except Exception as e:
        return f"Error: {e}"


async def mute(args: dict) -> str:
    """Mute or unmute the system audio."""
    on = args.get("on", True)
    if isinstance(on, str):
        on = on.strip().lower() in ("1", "true", "yes", "on", "y", "mute")
    try:
        return await asyncio.to_thread(_set_mute_sync, bool(on))
    except Exception as e:
        return f"Error: {e}"


async def set_brightness(args: dict) -> str:
    """Set the display brightness to a level 0-100 (laptop panels or DDC/CI monitors)."""
    try:
        level = _level(args)
    except (TypeError, ValueError):
        return "Error: set_brightness needs an integer 'level' 0-100."
    try:
        return await asyncio.to_thread(_set_brightness_sync, level)
    except Exception as e:
        return f"Error: {e}"


async def lock_pc(args: dict) -> str:
    """Lock the workstation (reversible — the user just signs back in)."""
    try:
        return await asyncio.to_thread(_lock_sync)
    except Exception as e:
        return f"Error: {e}"


async def media_control(args: dict) -> str:
    """Send a media key: playpause, next, previous, or stop."""
    action = str(args.get("action", "")).strip().lower()
    try:
        return await asyncio.to_thread(_media_sync, action)
    except Exception as e:
        return f"Error: {e}"


async def power_action(args: dict) -> str:
    """Sleep, shut down, or restart the PC (gated), or cancel a pending shutdown."""
    try:
        action = str(args.get("action", "")).strip().lower()
        if action == "cancel":
            return await asyncio.to_thread(_power_sync, "cancel", 0)
        if action not in ("sleep", "shutdown", "restart"):
            return "Error: action must be sleep, shutdown, restart, or cancel."
        delay = max(0, min(600, int(args.get("delay") or (0 if action == "sleep" else 15))))
        verb = {"sleep": "Sleep", "shutdown": "Shut down", "restart": "Restart"}[action]
        summary = f"{verb} this PC" + ("" if action == "sleep" else f" in {delay}s")
        ok = await confirm_cb(summary)
        if not ok:
            return f"User DENIED the {action}. Do not retry."
        return await asyncio.to_thread(_power_sync, action, delay)
    except Exception as e:
        return f"Error: {e}"


async def system_status(args: dict) -> str:
    """Report current volume (and mute) and display brightness."""
    try:
        return await asyncio.to_thread(_status_sync)
    except Exception as e:
        return f"Error: {e}"


def _schema(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


_LEVEL = {"type": "integer", "description": "Target level from 0 to 100"}


TOOLS: dict[str, dict] = {
    "set_volume": {
        "schema": _schema(
            "set_volume", "Set the PC's master volume (0-100). Also un-mutes if above 0.",
            {"level": _LEVEL}, ["level"]),
        "fn": set_volume,
    },
    "mute": {
        "schema": _schema(
            "mute", "Mute or unmute the PC audio.",
            {"on": {"type": "boolean", "description": "true = mute, false = unmute"}}, []),
        "fn": mute,
    },
    "set_brightness": {
        "schema": _schema(
            "set_brightness", "Set the display brightness (0-100). Works on laptop panels "
            "and DDC/CI-capable external monitors.",
            {"level": _LEVEL}, ["level"]),
        "fn": set_brightness,
    },
    "lock_pc": {
        "schema": _schema(
            "lock_pc", "Lock the PC (the user signs back in to unlock). No approval needed.",
            {}, []),
        "fn": lock_pc,
    },
    "media_control": {
        "schema": _schema(
            "media_control", "Send a media key to whatever is playing (Spotify, YouTube, "
            "etc.): playpause, next, previous, or stop.",
            {"action": {"type": "string",
                        "description": "playpause | next | previous | stop"}}, ["action"]),
        "fn": media_control,
    },
    "power_action": {
        "schema": _schema(
            "power_action", "Sleep, shut down, or restart the PC, or cancel a pending "
            "shutdown/restart. The system automatically asks the user to approve "
            "sleep/shutdown/restart on their phone — do NOT ask yourself, just call this. "
            "'cancel' aborts a countdown and needs no approval.",
            {"action": {"type": "string", "description": "sleep | shutdown | restart | cancel"},
             "delay": {"type": "integer",
                       "description": "Seconds before shutdown/restart (default 15)"}},
            ["action"]),
        "fn": power_action,
    },
    "system_status": {
        "schema": _schema(
            "system_status", "Report the PC's current volume, mute state, and brightness.",
            {}, []),
        "fn": system_status,
    },
}
