"""Configuration: config.yml + environment."""
import os
from pathlib import Path

import yaml

CONFIG_PATH = os.environ.get("DECKSHOTS_CONFIG", "/config/config.yml")
DATA = Path(os.environ.get("DECKSHOTS_DATA", "/opt/deckshots"))
INBOX, WORK, DBDIR = DATA / "inbox", DATA / "work", DATA / "db"
AGENT_DIR = Path(os.environ.get("DECKSHOTS_AGENT_DIR", "/app/agent"))
PKG_DIR = Path(os.environ.get("DECKSHOTS_PKG_DIR", "/app/pkg"))

with open(CONFIG_PATH) as fh:
    CFG = yaml.safe_load(fh) or {}

BOT_TOKEN = os.environ.get("BOT_TOKEN") or CFG.get("bot_token")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is required (env or config.yml)")
COMMAND_BOT_TOKEN = os.environ.get("COMMAND_BOT_TOKEN") or CFG.get("command_bot_token") or ""
# With a bot of your own, one token serves both delivery and commands. Set to false when the delivery bot's
# updates are consumed by another service (webhook/polling) and use COMMAND_BOT_TOKEN for a second bot instead.
COMMANDS_ON_MAIN = bool(CFG.get("commands_on_main_bot", True))
COMMANDS_TOKEN = COMMAND_BOT_TOKEN or (BOT_TOKEN if COMMANDS_ON_MAIN else "")

API = CFG.get("api_url", "http://telegram-bot-api:8081").rstrip("/")
LOCAL_FILES = bool(CFG.get("local_files", "telegram.org" not in API))
PREFIX = CFG.get("path_prefix", "/shots").rstrip("/")
MAX_PART = int(CFG.get("max_part_mb", 1900 if LOCAL_FILES else 49)) * 1024 * 1024
PHOTO_LIMIT = 10 * 1024 * 1024
SCREENSHOT_AS = CFG.get("screenshot_as", "photo")            # photo | document
STORE_LOOKUP = bool(CFG.get("steam_store_lookup", True))
RETRY_MAX = int(CFG.get("retry_max_seconds", 600))
ADMIN_CHAT = CFG.get("admin_chat_id")
DL_KEY = str(CFG.get("dl_key", ""))
ALBUM_WAIT = int(CFG.get("album_wait_seconds", 45))          # screenshots of one game within this window go as an album
ALBUM_MAX = max(1, min(10, int(CFG.get("album_max", 10))))   # 1 disables albums
WORKERS_FAST = int(CFG.get("workers_fast", 2))
WORKERS_SLOW = int(CFG.get("workers_slow", 1))
TRANSCODE_PRESET = CFG.get("transcode_preset", "veryfast")
LANG = CFG.get("language", "ru")

DEVICES = {d["token"]: d for d in CFG.get("devices", [])}
BY_NAME = {d["name"]: d for d in DEVICES.values()}


def devices_for_chat(chat_id) -> list[dict]:
    return [d for d in DEVICES.values() if d.get("chat_id") == chat_id]


def is_admin(chat_id) -> bool:
    return bool(ADMIN_CHAT) and chat_id == ADMIN_CHAT
