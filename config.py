# -*- coding: utf-8 -*-
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent

# =============================
# Telegram
# =============================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
APP_NAME = os.getenv("APP_NAME", "SetupBridge").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in .env")

# =============================
# Google
# =============================
GOOGLE_CLIENT_SECRET_FILE = os.getenv(
    "GOOGLE_CLIENT_SECRET_FILE",
    "client_secret.json"
)

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets"
]

# =============================
# Storage Paths
# =============================
DATA_DIR = BASE_DIR / "data"
TMP_DIR = BASE_DIR / "tmp"

DATA_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)

STATE_PATH = str(DATA_DIR / "state.json")

# =============================
# Default Folders
# =============================
FOLDER_PHOTOS = "Photos"
FOLDER_VIDEOS = "Videos"
FOLDER_DOCS = "Documents"
FOLDER_AUDIO = "Audio"
FOLDER_VOICE = "Voice"
FOLDER_STICKERS = "Stickers"
FOLDER_OTHER = "Other"
FOLDER_TEXTS = "Text Archive"

# =============================
# Defaults
# =============================
DEFAULT_KEYWORDS = []
