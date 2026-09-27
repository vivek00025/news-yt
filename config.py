import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data")).resolve()
ASSETS_DIR = DATA_DIR / "assets"
RUNS_DIR = DATA_DIR / "runs"
DB_PATH = DATA_DIR / "newsreel.db"
for _d in (DATA_DIR, ASSETS_DIR, RUNS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "5000"))
BASE_URL = os.getenv("BASE_URL", f"http://localhost:{PORT}").rstrip("/")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

MANUS_BASE_URL = os.getenv("MANUS_BASE_URL", "https://api.manus.ai").rstrip("/")
MANUS_POLL_SECONDS = int(os.getenv("MANUS_POLL_SECONDS", "10"))
MANUS_TIMEOUT_MINUTES = int(os.getenv("MANUS_TIMEOUT_MINUTES", "30"))

_gcs = os.getenv("GOOGLE_CLIENT_SECRETS_FILE", "client_secret.json")
GOOGLE_CLIENT_SECRETS_FILE = Path(_gcs) if Path(_gcs).is_absolute() else BASE_DIR / _gcs

MAX_PARALLEL_RUNS = int(os.getenv("MAX_PARALLEL_RUNS", "3"))   # videos generated at the same time (one per client)
TICK_SECONDS = int(os.getenv("TICK_SECONDS", "30"))            # how often the scheduler looks for work
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))             # generation attempts per video
MAX_LATE_HOURS = float(os.getenv("MAX_LATE_HOURS", "6"))       # drop videos that could not go out within this
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "10"))        # keep finished video files this long
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe")

# Output frame sizes for each supported aspect ratio
FRAME_SIZES = {"9:16": (1080, 1920), "16:9": (1920, 1080)}
DURATION_MIN, DURATION_MAX = 5, 180   # seconds; presets are 10/20/30, anything in range is allowed
MAX_VIDEOS_PER_DAY = 4
