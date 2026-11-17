"""Step 1 — get the source video into work/<id>/ (YouTube/any yt-dlp URL, or a local file)."""
from __future__ import annotations

import shutil
from pathlib import Path

from .util import log, read_json, slug, write_json


def _existing_source(workdir: Path) -> Path | None:
    for p in workdir.glob("source.*"):
        if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}:
            return p
    return None


def fetch(source: str, cfg: dict) -> Path:
    """Return the work directory for this video, downloading/copying the source if needed."""
    root = Path(cfg["workdir"])
    root.mkdir(parents=True, exist_ok=True)

    local = Path(source)
    if local.exists():
        workdir = root / slug(local.stem)
        workdir.mkdir(parents=True, exist_ok=True)
        if not _existing_source(workdir):
            dest = workdir / f"source{local.suffix.lower()}"
            log(f"copying {local} -> {dest}")
            shutil.copy2(local, dest)
        meta = read_json(workdir / "meta.json", {}) or {}
        meta.update({"title": meta.get("title") or local.stem, "url": meta.get("url", "")})
        write_json(workdir / "meta.json", meta)
        return workdir

    from yt_dlp import YoutubeDL

    with YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
        info = ydl.extract_info(source, download=False)
    workdir = root / slug(info.get("id") or info.get("title", "video"))
    workdir.mkdir(parents=True, exist_ok=True)
    write_json(
        workdir / "meta.json",
        {
            "id": info.get("id"),
            "title": info.get("title"),
            "url": info.get("webpage_url", source),
            "channel": info.get("channel") or info.get("uploader"),
            "duration": info.get("duration"),
        },
    )
    if _existing_source(workdir):
        log("source already downloaded — skipping")
        return workdir

    log(f"downloading: {info.get('title')}")
    opts = {
        "format": cfg["download"]["format"],
        "outtmpl": str(workdir / "source.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": False,
        "socket_timeout": 60,
        "retries": 15,
        "fragment_retries": 15,
        "file_access_retries": 5,
        "continuedl": True,
        "concurrent_fragment_downloads": 4,
        "http_chunk_size": 10 * 1024 * 1024,
    }
    with YoutubeDL(opts) as ydl:
        ydl.download([source])
    if not _existing_source(workdir):
        raise RuntimeError("download finished but no source file found")
    return workdir


def source_path(workdir: Path) -> Path:
    p = _existing_source(Path(workdir))
    if not p:
        raise FileNotFoundError(f"no source.* video in {workdir}")
    return p
