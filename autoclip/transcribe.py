"""Step 2 — transcript with word-level timestamps.

Engines (config transcribe.engine):
  auto            youtube captions -> mlx-whisper (Apple Silicon) -> faster-whisper
  youtube         YouTube's own captions (instant, needs a YouTube URL with captions)
  mlx             mlx-whisper on the Apple GPU (pip install mlx-whisper)
  faster-whisper  CPU/CUDA whisper (works everywhere)
"""
from __future__ import annotations

import json
import platform
import subprocess
from pathlib import Path

from .download import source_path
from .util import log, read_json, write_json

MLX_REPOS = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "turbo": "mlx-community/whisper-large-v3-turbo",
}


def load_audio(path: Path, sr: int = 16000):
    """Decode audio with the ffmpeg CLI (avoids PyAV version issues)."""
    import numpy as np

    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


# ---------------------------------------------------------------- 1. YouTube captions
def _pick_track(info: dict, lang: str | None) -> tuple[str, dict] | None:
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    order: list[tuple[str, dict]] = []
    if lang:
        order += [(lang, manual), (f"{lang}-orig", auto), (lang, auto)]
    order += [(k, auto) for k in auto if k.endswith("-orig")]  # original-language ASR
    order += [(k, manual) for k in manual if k != "live_chat"]
    for key, src in order:
        formats = src.get(key) or []
        j3 = next((f for f in formats if f.get("ext") == "json3"), None)
        if j3:
            return key, j3
    return None


def parse_json3(data: dict) -> dict:
    """YouTube json3 captions -> our transcript format (auto captions carry per-word offsets)."""
    segments = []
    events = [e for e in data.get("events", []) if e.get("segs")]
    for i, ev in enumerate(events):
        start = ev.get("tStartMs", 0) / 1000
        end = start + ev.get("dDurationMs", 0) / 1000
        if i + 1 < len(events):
            end = min(end, events[i + 1].get("tStartMs", 0) / 1000) or end
        parts = [(s.get("tOffsetMs", 0) / 1000, s.get("utf8", "")) for s in ev["segs"]]
        tokens = [(start + off, t.strip()) for off, t in parts if t.strip()]
        if not tokens:
            continue
        has_offsets = any(off > 0 for off, _ in parts[1:])
        words = []
        if has_offsets:
            for j, (ws, t) in enumerate(tokens):
                we = tokens[j + 1][0] if j + 1 < len(tokens) else end
                for k, w in enumerate(t.split()):  # rare multi-word seg
                    words.append({"start": ws, "end": min(max(we, ws + 0.05), ws + 1.2), "word": w})
        else:  # manual subs: spread the words evenly over the cue
            flat = " ".join(t for _, t in tokens).split()
            step = max(0.05, (end - start) / max(1, len(flat)))
            words = [{"start": start + k * step, "end": start + (k + 1) * step, "word": w} for k, w in enumerate(flat)]
        words = [dict(w, start=round(w["start"], 3), end=round(w["end"], 3)) for w in words]
        segments.append({
            "start": words[0]["start"], "end": round(max(end, words[-1]["end"]), 3),
            "text": " ".join(w["word"] for w in words), "words": words,
        })
    return {"segments": segments}


def from_youtube(workdir: Path, cfg: dict) -> dict | None:
    meta = read_json(Path(workdir) / "meta.json", {}) or {}
    url = meta.get("url") or ""
    if "youtu" not in url:
        return None
    try:
        from yt_dlp import YoutubeDL

        lang = cfg["transcribe"].get("language")
        with YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            track = _pick_track(info, lang)
            if not track:
                log("no YouTube captions available")
                return None
            key, fmt = track
            log(f"using YouTube captions ({key})")
            raw = ydl.urlopen(fmt["url"]).read().decode("utf-8")
        data = parse_json3(json.loads(raw))
        if len(data["segments"]) < 5:
            log("YouTube captions look empty")
            return None
        data["language"] = key.split("-")[0]
        data["duration"] = info.get("duration") or data["segments"][-1]["end"]
        data["engine"] = f"youtube:{key}"
        return data
    except Exception as e:  # network, format change, rate limit…
        log(f"YouTube captions failed ({e.__class__.__name__}: {str(e)[:120]})")
        return None


# ---------------------------------------------------------------- 2. mlx-whisper (Apple GPU)
def _is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def from_mlx(workdir: Path, cfg: dict, required: bool = False) -> dict | None:
    try:
        import mlx_whisper
    except ImportError:
        if required:
            raise RuntimeError("mlx-whisper not installed — run: pip install mlx-whisper")
        log("mlx-whisper not installed (pip install mlx-whisper) — skipping")
        return None
    t = cfg["transcribe"]
    repo = t.get("mlx_model") or MLX_REPOS.get(t["model"], f"mlx-community/whisper-{t['model']}-mlx")
    log(f"transcribing with mlx-whisper on the Apple GPU ({repo}; first run downloads it)")
    audio = load_audio(source_path(workdir))
    res = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo=repo,
        language=t.get("language") or None,
        task=t.get("task", "transcribe"),
        word_timestamps=True,
        verbose=False,
    )
    segments = []
    for seg in res.get("segments", []):
        words = [
            {"start": round(w["start"], 3), "end": round(w["end"], 3), "word": w["word"].strip()}
            for w in seg.get("words", []) if w["word"].strip()
        ]
        segments.append({"start": round(seg["start"], 3), "end": round(seg["end"], 3),
                         "text": seg["text"].strip(), "words": words})
    return {"language": res.get("language"), "duration": len(audio) / 16000,
            "segments": segments, "engine": f"mlx:{repo}"}


# ---------------------------------------------------------------- 3. faster-whisper (CPU / CUDA)
def from_faster_whisper(workdir: Path, cfg: dict) -> dict:
    from faster_whisper import WhisperModel

    t = cfg["transcribe"]
    log(f"loading faster-whisper '{t['model']}' (first run downloads it)")
    model = WhisperModel(t["model"], device=t["device"], compute_type=t["compute_type"])
    log("extracting audio")
    audio = load_audio(source_path(workdir))
    segments, info = model.transcribe(
        audio,
        language=t.get("language") or None,
        task=t.get("task", "transcribe"),
        word_timestamps=True,
        vad_filter=True,
    )
    log(f"detected language: {info.language} — transcribing {info.duration:.0f}s of audio")
    data = {"language": info.language, "duration": info.duration, "segments": [], "engine": "faster-whisper"}
    for seg in segments:
        words = [
            {"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word.strip()}
            for w in (seg.words or []) if w.word.strip()
        ]
        data["segments"].append(
            {"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text.strip(), "words": words}
        )
        print(f"  [{seg.start:7.1f} / {info.duration:.0f}] {seg.text.strip()[:80]}", flush=True)
    return data


# ---------------------------------------------------------------- entry point
def transcribe(workdir: Path, cfg: dict, force: bool = False) -> dict:
    out = Path(workdir) / "transcript.json"
    if out.exists() and not force:
        log("transcript exists — skipping")
        return read_json(out)

    engine = (cfg["transcribe"].get("engine") or "auto").lower()
    data = None
    if engine in ("auto", "youtube"):
        data = from_youtube(workdir, cfg)
        if data is None and engine == "youtube":
            raise RuntimeError("no usable YouTube captions — set transcribe.engine: auto")
    if data is None and (engine == "mlx" or (engine == "auto" and _is_apple_silicon())):
        data = from_mlx(workdir, cfg, required=engine == "mlx")
    if data is None:
        data = from_faster_whisper(workdir, cfg)

    write_json(out, data)
    log(f"saved {out} ({len(data['segments'])} segments via {data.get('engine')})")
    return data


def all_words(transcript: dict) -> list[dict]:
    return [w for s in transcript["segments"] for w in s.get("words", [])]
