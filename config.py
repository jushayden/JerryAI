"""Central config for Pocket Agent. Values come from .env where secret."""
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

# --- Telegram ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ALLOWED_CHAT_ID = int(os.getenv("ALLOWED_CHAT_ID", "0"))  # 0 = capture mode: first /start sets it

# --- Model ---
MODEL = os.getenv("MODEL", "qwen3-coder:30b")
FALLBACK_MODELS = ["gpt-oss:20b", "qwen3:14b"]
NUM_CTX = 16384
TEMPERATURE = 0.2
KEEP_ALIVE = -1
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

# --- Agent loop bounds ---
MAX_STEPS = 15
MODEL_CALL_TIMEOUT = 120  # seconds per model call; human waits (ask/confirm) are NOT capped
                          # by a task clock — MAX_STEPS + this bound the machine time instead
CONFIRM_TIMEOUT = 300   # seconds waiting for phone approval; timeout == deny
TOOL_RESULT_MAX = 2000  # chars per tool result fed back to the model
PAGE_TEXT_MAX = 3000

# --- PC control ---
SANDBOX_ROOT = Path(os.getenv("SANDBOX_ROOT", str(Path.home())))  # fs tools confined here


def _desktop() -> Path:
    """The user's real (possibly OneDrive-redirected) Desktop."""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            return Path(os.path.expandvars(winreg.QueryValueEx(k, "Desktop")[0]))
    except Exception:
        return Path.home() / "Desktop"


DESKTOP = _desktop()
APP_ALLOWLIST = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "explorer": "explorer.exe",
    "paint": "mspaint.exe",
    "chrome": "chrome",
    "edge": "msedge",
}
BROWSER_PROFILE_DIR = PROJECT_ROOT / "browser_profile"

# --- Data files ---
PROFILE_PATH = PROJECT_ROOT / "profile.yaml"
PROFILE_EXTRA_PATH = PROJECT_ROOT / "profile_extra.yaml"  # facts the user gives via Telegram
EVENTS_LOG = PROJECT_ROOT / "events.jsonl"
SCREENSHOT_DIR = PROJECT_ROOT / "screenshots"

# --- Co-drive: attach to the user's real Edge via CDP when available ---
CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"

# --- Mock form server ---
MOCK_FORM_PORT = 8000
MOCK_FORM_DIR = PROJECT_ROOT / "mock_form"

# --- Local endpoint for the Edge J-badge extension (127.0.0.1 only) ---
LOCAL_PORT = 8765
LOCAL_TOKEN = os.getenv("LOCAL_TOKEN", "pocket-agent-local")

# --- Social (Track C): Reddit + YouTube read-only search, official free APIs ---
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "pocket-agent:v1 (by /u/change_me)")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
