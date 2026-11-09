"""
Configuration loader for ZKTeco ADMS Server.
Reads settings from environment variables or a local .env file.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from current directory if present
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Server Network Settings
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8080"))

# Timezone Shift Settings
# If DATE_HEADER_SHIFT_HOURS is provided directly, use it; otherwise compute from offsets.
_device_offset = float(os.getenv("DEVICE_UTC_OFFSET_HOURS", "8.0"))
_server_offset = float(os.getenv("SERVER_UTC_OFFSET_HOURS", "5.5"))
_default_shift = _device_offset - _server_offset

DATE_HEADER_SHIFT_HOURS = float(os.getenv("DATE_HEADER_SHIFT_HOURS", str(_default_shift)))

# Parse per-device overrides (e.g. SN1=2.5,SN2=4.0)
DATE_HEADER_SHIFT_SN: dict[str, float] = {}
_raw_sn_shifts = os.getenv("DATE_HEADER_SHIFT_SN", "")
if _raw_sn_shifts:
    for entry in _raw_sn_shifts.replace(";", ",").split(","):
        entry = entry.strip()
        if not entry:
            continue
        sn, _, hours = entry.rpartition("=")
        try:
            DATE_HEADER_SHIFT_SN[sn.strip()] = float(hours.strip())
        except ValueError:
            pass

# Database Settings
DB_ENGINE = os.getenv("DB_ENGINE", "sqlite").lower()
DB_SQLITE_PATH = os.getenv("DB_SQLITE_PATH", str(BASE_DIR / "attendance.db"))

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASS = os.getenv("DB_PASS", "")
DB_NAME = os.getenv("DB_NAME", "attendance_db")

# Webhook Forwarding Settings
WEBHOOK_ENABLED = os.getenv("WEBHOOK_ENABLED", "false").lower() in ("true", "1", "yes")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_BEARER_TOKEN = os.getenv("WEBHOOK_BEARER_TOKEN", "")
WEBHOOK_TIMEOUT = int(os.getenv("WEBHOOK_TIMEOUT", "15"))
WEBHOOK_BATCH_SIZE = int(os.getenv("WEBHOOK_BATCH_SIZE", "50"))

