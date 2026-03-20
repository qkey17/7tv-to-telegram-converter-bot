import os
from pathlib import Path

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

SAVE_ROOT = Path("downloads")
SAVE_ROOT.mkdir(exist_ok=True)

API_TEMPLATE = "https://api.7tv.app/v3/emote-sets/{set_id}"
EMOTE_API_TEMPLATES = [
    "https://api.7tv.app/v3/emotes/{emote_id}",
    "https://7tv.io/v3/emotes/{emote_id}",
]
CDN_BASE = "https://cdn.7tv.app/emote/{id}/{file}"

if not BOT_TOKEN:
    raise ValueError("Не найден TELEGRAM_BOT_TOKEN в переменных среды")
