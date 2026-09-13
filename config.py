"""Central config for Tora AI. Values come from .env where secret."""
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

# --- Telegram ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ALLOWED_CHAT_ID = int(os.getenv("ALLOWED_CHAT_ID") or 0)  # 0 = unpaired; /start displays ID only

# --- Model ---
MODEL = os.getenv("MODEL", "qwen3-coder:30b")
FALLBACK_MODELS = ["gpt-oss:20b", "qwen3:14b"]
NUM_CTX = 16384
TEMPERATURE = 0.2
KEEP_ALIVE = -1
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
VISION_ENABLED = os.getenv("VISION_ENABLED", "1").lower() not in ("0", "false", "no")
VISION_MODEL = os.getenv("VISION_MODEL", "qwen3-vl:30b-a3b-instruct")
VISION_MIN_CONFIDENCE = float(os.getenv("VISION_MIN_CONFIDENCE", "0.55"))
VISION_MAX_ATTEMPTS = 2

# --- Agent loop bounds ---
MAX_STEPS = 25
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
EDGE_CODRIVE_PROFILE_DIR = Path(os.getenv(
    "EDGE_CODRIVE_PROFILE_DIR",
    str(Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "JerryAI" / "EdgeProfile"),
))

# --- Data files ---
PROFILE_PATH = PROJECT_ROOT / "profile.yaml"
PROFILE_EXTRA_PATH = PROJECT_ROOT / "profile_extra.yaml"  # facts the user gives via Telegram
EVENTS_LOG = PROJECT_ROOT / "events.jsonl"
SCREENSHOT_DIR = PROJECT_ROOT / "screenshots"
ARTIFACT_DIR = PROJECT_ROOT / "artifacts"
DOWNLOAD_DIR = ARTIFACT_DIR / "downloads"

# --- Co-drive: attach to the user's real Edge via CDP when available ---
CDP_PORT = int(os.getenv("CDP_PORT") or 9222)  # override when another app already owns 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
BROWSER_MODE = os.getenv("BROWSER_MODE", "edge").lower()  # edge in production; owned in tests
EDGE_START_TIMEOUT = float(os.getenv("EDGE_START_TIMEOUT", "20"))

# --- Mock form server ---
MOCK_FORM_PORT = 8000
MOCK_FORM_DIR = PROJECT_ROOT / "mock_form"
MOCK_FORM_ENABLED = os.getenv("MOCK_FORM_ENABLED", "0").lower() in ("1", "true", "yes")

# --- Local endpoint for the Edge T-badge extension (127.0.0.1 only) ---
LOCAL_PORT = 8765
LOCAL_TOKEN_PATH = PROJECT_ROOT / ".local-token"
LOCAL_TOKEN = os.getenv("LOCAL_TOKEN", "").strip()
if not LOCAL_TOKEN and LOCAL_TOKEN_PATH.is_file():
    LOCAL_TOKEN = LOCAL_TOKEN_PATH.read_text(encoding="utf-8").strip()
MAX_QUEUED_TASKS = 20


def ensure_local_token() -> str:
    """Create a per-install pairing key once; no file writes during module import."""
    global LOCAL_TOKEN
    if LOCAL_TOKEN:
        if LOCAL_TOKEN == "pocket-agent-local" or len(LOCAL_TOKEN) < 32:
            raise ValueError("LOCAL_TOKEN must be a private key of at least 32 characters. "
                             "Remove the old override and run scripts/setup.py to generate one.")
        return LOCAL_TOKEN
    try:
        fd = os.open(LOCAL_TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        LOCAL_TOKEN = LOCAL_TOKEN_PATH.read_text(encoding="utf-8").strip()
        if not LOCAL_TOKEN:
            raise ValueError(".local-token is empty; remove it and run scripts/setup.py again.")
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secrets.token_urlsafe(32) + "\n")
        LOCAL_TOKEN = LOCAL_TOKEN_PATH.read_text(encoding="utf-8").strip()
    return ensure_local_token()
