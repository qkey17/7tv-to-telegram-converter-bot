import requests
from pathlib import Path

def download_file(url: str, path: Path):
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        path.write_bytes(r.content)
        return True
    except requests.RequestException:
        return False