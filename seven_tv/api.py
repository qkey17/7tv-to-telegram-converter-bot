import re
import requests
from config import API_TEMPLATE

def extract_set_id(text: str):
    m = re.search(r"7tv\.app\/emote-sets\/([A-Za-z0-9]+)", text)
    return m.group(1) if m else None


def fetch_emote_list(set_id: str):
    try:
        r = requests.get(API_TEMPLATE.format(set_id=set_id), timeout=20)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return None


def get_best_file(files):
    webp_files = [f for f in files if f.get("format") == "WEBP"]
    if not webp_files:
        return None
    best = max(webp_files, key=lambda x: x.get("size", 0))
    return best.get("name")