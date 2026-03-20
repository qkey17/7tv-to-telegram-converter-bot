import re
import requests

from config import API_TEMPLATE, EMOTE_API_TEMPLATES

SET_LINK_RE = re.compile(r"7tv\.app/emote-sets/([A-Za-z0-9]+)")
EMOTE_LINK_RE = re.compile(r"7tv\.app/emotes/([A-Za-z0-9]+)")


def extract_set_id(text: str):
    m = SET_LINK_RE.search(text)
    return m.group(1) if m else None


def extract_emote_id(text: str):
    m = EMOTE_LINK_RE.search(text)
    return m.group(1) if m else None


def fetch_emote_list(set_id: str):
    try:
        r = requests.get(API_TEMPLATE.format(set_id=set_id), timeout=20)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return None


def fetch_emote(emote_id: str):
    for template in EMOTE_API_TEMPLATES:
        try:
            r = requests.get(template.format(emote_id=emote_id), timeout=20)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            continue
    return None


def get_best_file(files):
    webp_files = [f for f in files if f.get("format") == "WEBP"]
    if not webp_files:
        return None
    best = max(webp_files, key=lambda x: x.get("size", 0))
    return best.get("name")


def unwrap_emote(payload):
    if not isinstance(payload, dict):
        return None

    if isinstance(payload.get("data"), dict):
        return payload["data"]

    if isinstance(payload.get("emote"), dict):
        return payload["emote"]

    return payload if "host" in payload else None
