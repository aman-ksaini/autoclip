"""Extra signals that tell us which moments are interesting — beyond the words themselves.

  heatmap    YouTube "Most replayed" graph (real viewer behaviour, 0..1 per time slice)
  reactions  audience laughter / applause / shouting = sudden loudness jumps in the audio,
             plus caption tags like [laughter], [हंसी], [प्रशंसा]
Cached in signals.json.
"""
from __future__ import annotations

import re
from pathlib import Path

from .download import source_path
from .util import log, read_json, write_json

REACTION_TAGS = re.compile(
    r"\[(?:laughter|laughs|laughing|applause|cheering|cheers|music|हंसी|हँसी|प्रशंसा|तालियां|तालियाँ|"
    r"हौसला बढ़ाने की आवाज़|चीखने की आवाज़)[^\]]*\]",
    re.I,
)


def _heatmap(workdir: Path) -> list[dict]:
    meta = read_json(workdir / "meta.json", {}) or {}
    if "heatmap" in meta:
        return meta["heatmap"] or []
    url = meta.get("url") or ""
    if "youtu" not in url:
        return []
    try:
        from yt_dlp import YoutubeDL

        with YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        hm = info.get("heatmap") or []
        meta["heatmap"] = [
            {"start": round(h["start_time"], 2), "end": round(h["end_time"], 2), "value": round(h["value"], 3)}
            for h in hm
        ]
        write_json(workdir / "meta.json", meta)
        return meta["heatmap"]
    except Exception as e:
        log(f"heatmap unavailable ({e.__class__.__name__})")
        return []


def _audio_reactions(workdir: Path) -> list[dict]:
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view

    from .transcribe import load_audio

    audio = load_audio(source_path(workdir))
    win = 8000  # 0.5 s at 16 kHz
    n = len(audio) // win
    if n < 120:
        return []
    frames = audio[: n * win].reshape(n, win)
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-6)
    pad = 30  # ±15 s local baseline
    padded = np.pad(db, pad, mode="edge")
    baseline = np.median(sliding_window_view(padded, 2 * pad + 1), axis=1)[:n]
    hot = (db > baseline + 6.0) & (db > -40)
    events, i = [], 0
    while i < n:
        if hot[i]:
            j = i
            while j + 1 < n and (hot[j + 1] or (j + 2 < n and hot[j + 2])):
                j += 1
            if j - i + 1 >= 2:  # at least ~1 s
                events.append({
                    "start": round(i * 0.5, 1), "end": round((j + 1) * 0.5, 1),
                    "strength": round(float((db[i: j + 1] - baseline[i: j + 1]).max()), 1),
                })
            i = j + 1
        else:
            i += 1
    return events


def _text_reactions(transcript: dict) -> list[dict]:
    out = []
    for s in transcript.get("segments", []):
        if REACTION_TAGS.search(s.get("text", "")):
            out.append({"start": s["start"], "end": s["end"], "strength": 6.0, "tag": True})
    return out


def get_signals(workdir: Path, transcript: dict, cfg: dict) -> dict:
    workdir = Path(workdir)
    cache = workdir / "signals.json"
    if cache.exists():
        return read_json(cache)
    p = cfg["pick"]
    sig = {"heatmap": [], "reactions": []}
    if p.get("use_heatmap", True):
        sig["heatmap"] = _heatmap(workdir)
        log(f"YouTube most-replayed data: {'yes' if sig['heatmap'] else 'not available'}")
    if p.get("use_audio", True):
        try:
            sig["reactions"] = _audio_reactions(workdir)
        except Exception as e:
            log(f"audio analysis skipped ({e.__class__.__name__})")
    sig["reactions"] += _text_reactions(transcript)
    log(f"audience reactions / loud moments found: {len(sig['reactions'])}")
    write_json(cache, sig)
    return sig


def heat_at(sig: dict, t0: float, t1: float) -> float:
    """Average most-replayed value (0..1) over a time range; 0 if unknown."""
    vals = [h["value"] for h in sig.get("heatmap", []) if h["end"] > t0 and h["start"] < t1]
    return sum(vals) / len(vals) if vals else 0.0


def reactions_in(sig: dict, t0: float, t1: float) -> list[dict]:
    return [r for r in sig.get("reactions", []) if r["end"] > t0 and r["start"] < t1]
