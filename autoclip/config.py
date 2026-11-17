"""Config loading: defaults <- config.yaml <- CLI overrides."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

DEFAULTS: dict = {
    "workdir": "work",
    "download": {"format": "bv*[height<=1080]+ba/b[height<=1080]"},
    "transcribe": {
        "engine": "auto",
        "model": "small",
        "language": "hi",
        "task": "transcribe",
        "device": "auto",
        "compute_type": "default",
    },
    "pick": {
        "provider": "anthropic",
        "model": "claude-sonnet-5-5",
        "base_url": None,
        "count": None,
        "max_clips": 15,
        "min_clips": 3,
        "min_score": 70,
        "min_seconds": None,
        "max_seconds": None,
        "video_type": "",
        "context": "",
        "plan": True,
        "max_strategies": 3,
        "max_candidates": 30,
        "audience": "Hindi/Hinglish-speaking audience on YouTube Shorts",
        "avoid": "explicit sexual jokes, slurs, abuse",
        "window_chars": 60000,
        "candidates_factor": 2,
        "refine": True,
        "boundary_review": True,
        "use_heatmap": True,
        "use_audio": True,
        "instructions": "",
    },
    "render": {
        "width": 1080,
        "height": 1920,
        "reframe": "auto",
        "multi_speaker": "auto",
        "group_style": "auto",
        "analysis_fps": 5,
        "min_shot_seconds": 1.2,
        "min_face_ratio": 0.045,
        "models_dir": "models",
        "crf": 20,
        "preset": "veryfast",
        "captions": True,
        "caption_font": "Noto Sans Devanagari",
        "caption_size": 80,
        "caption_uppercase": True,
        "words_per_line": 3,
        "caption_margin_v": 520,
        "highlight_color": "&H0000FFFF&",
        "hook": True,
        "headline": "intro",
        "teaser": True,
        "punch_in": 1.15,
        "jumpcut_gap": "auto",
        "keyword_color": "&H0000FF7F&",
        "progress_bar": False,
        "hook_seconds": 3,
        "hook_size": 72,
        "take_when": "end",
        "take_seconds": 4,
        "take_size": 42,
        "take_margin_v": 380,
        "take_label": "",
        "fontsdir": "fonts",
    },
    "channel": {
        "name": "",
        "voice": "witty, warm, reacts like a friend sharing a moment",
        "language": "",
        "signoff": "",
        "question": True,
    },
    "package": {"enabled": True},
    "youtube": {
        "client_secrets": "client_secret.json",
        "token": "token.json",
        "privacy": "private",
        "category_id": "24",
        "made_for_kids": False,
        "add_shorts_tag": True,
        "credit": "🎥 Original: {source_channel}",
        "schedule": {"enabled": False, "start": None, "every_hours": 24},
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None = "config.yaml") -> dict:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    user = {}
    if path and Path(path).exists():
        user = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return _merge(DEFAULTS, user)
