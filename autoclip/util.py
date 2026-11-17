from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


def log(msg: str) -> None:
    print(f"[autoclip] {msg}", flush=True)


def read_json(path: Path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def ffprobe(path: Path) -> dict:
    """Return width, height, duration of the first video stream."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    st = data["streams"][0]
    return {
        "width": int(st["width"]),
        "height": int(st["height"]),
        "duration": float(data["format"]["duration"]),
    }


def fmt_ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF‍️]+"
)


def strip_emoji(text: str) -> str:
    return _EMOJI.sub("", text).strip()


def slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^\w\-]+", "-", text, flags=re.UNICODE).strip("-").lower()
    return s[:n] or "video"
