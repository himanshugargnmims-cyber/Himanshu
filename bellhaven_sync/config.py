import os
from pathlib import Path

BASE_URL = os.environ.get(
    "BELLHAVEN_BASE_URL", "https://analyst-assessment-production.up.railway.app"
).rstrip("/")
API_BASE = f"{BASE_URL}/api/v1"

# Never hard-code the token: this repo is public.
API_TOKEN = os.environ.get("BELLHAVEN_API_TOKEN", "")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("BELLHAVEN_DB", PROJECT_ROOT / "state" / "bellhaven.db"))

# Tag written into CRM notes so every automated change is traceable.
NOTE_TAG = "[bellhaven-sync]"
