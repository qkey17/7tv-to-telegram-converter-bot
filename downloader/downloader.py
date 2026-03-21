from __future__ import annotations

import requests
from pathlib import Path


def download_file(url: str, path: Path, cancel_event=None) -> bool:
    try:
        with requests.get(url, stream=True, timeout=30) as response:
            response.raise_for_status()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as f:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if cancel_event is not None and cancel_event.is_set():
                        if path.exists():
                            try:
                                path.unlink()
                            except Exception:
                                pass
                        return False
                    if chunk:
                        f.write(chunk)
        return True
    except requests.RequestException:
        if path.exists():
            try:
                path.unlink()
            except Exception:
                pass
        return False
