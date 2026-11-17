"""Step 5 — upload rendered clips to YouTube as Shorts (OAuth, resumable, optional scheduling)."""
from __future__ import annotations

import os
import random
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .util import log, read_json, write_json

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
RETRY_STATUS = {500, 502, 503, 504}


def get_service(cfg: dict):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    y = cfg["youtube"]
    token = Path(y["token"])
    creds = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        secrets = Path(y["client_secrets"])
        if not secrets.exists():
            raise FileNotFoundError(
                f"{secrets} not found — create an OAuth 'Desktop app' client in Google Cloud "
                "and download its JSON (see README)"
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
    token.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds)


def _title(clip: dict, cfg: dict) -> str:
    pkg = clip.get("package") or {}
    t = (pkg.get("yt_title") or clip.get("title") or clip["id"]).strip()
    if cfg["youtube"].get("add_shorts_tag") and "#shorts" not in t.lower():
        t = f"{t} #Shorts" if len(t) <= 92 else t
    return t[:100]


def _hashtags(clip: dict) -> list[str]:
    return (clip.get("package") or {}).get("hashtags") or clip.get("hashtags", [])


def _description(clip: dict, meta: dict, cfg: dict) -> str:
    pkg = clip.get("package") or {}
    tags = " ".join(_hashtags(clip))
    parts = [(pkg.get("yt_description") or clip.get("description", "")).strip(), tags]
    credit = cfg["youtube"].get("credit")
    if credit and (meta.get("title") or meta.get("url") or meta.get("channel")):
        parts.append(credit.format(source_title=meta.get("title", ""), source_url=meta.get("url", ""),
                                   source_channel=meta.get("channel") or meta.get("title", "")).strip())
    return "\n\n".join(p for p in parts if p)[:4900]


def ready_file(workdir: Path, clip: dict) -> Path:
    """The rendered clip under its topic name (work/<id>/ready/<slug>.mp4), so no 'clip_01' anywhere."""
    src = workdir / "clips" / f"{clip['id']}.mp4"
    name = (clip.get("package") or {}).get("slug")
    if not name or not src.exists():
        return src
    out_dir = workdir / "ready"
    out_dir.mkdir(exist_ok=True)
    dst = out_dir / f"{name}.mp4"
    if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
        if dst.exists():
            dst.unlink()
        try:
            os.link(src, dst)  # no extra disk space
        except OSError:
            shutil.copy2(src, dst)
    return dst


def _tags(clip: dict) -> list[str]:
    tags, size = [], 0
    for h in _hashtags(clip):
        t = h.lstrip("#").strip()
        if t and size + len(t) + 2 < 450:
            tags.append(t)
            size += len(t) + 2
    return tags


def _publish_times(n: int, cfg: dict) -> list[str | None]:
    s = cfg["youtube"].get("schedule") or {}
    if not s.get("enabled"):
        return [None] * n
    start = datetime.fromisoformat(s["start"]) if s.get("start") else datetime.now(timezone.utc) + timedelta(hours=1)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    step = timedelta(hours=float(s.get("every_hours", 24)))
    return [(start + i * step).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(n)]


def _upload_one(service, path: Path, body: dict) -> str:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)
    response, attempt = None, 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"    {int(status.progress() * 100)}%", end="\r", flush=True)
        except HttpError as e:
            if e.resp.status not in RETRY_STATUS or attempt >= 6:
                raise
            attempt += 1
        except (OSError, ConnectionError):
            if attempt >= 6:
                raise
            attempt += 1
        else:
            continue
        wait = random.uniform(1, 2 ** attempt)
        log(f"upload hiccup, retry {attempt} in {wait:.0f}s")
        time.sleep(wait)
    return response["id"]


def upload_all(workdir: Path, cfg: dict, only: list[str] | None = None) -> dict:
    workdir = Path(workdir)
    clips = read_json(workdir / "clips.json") or []
    meta = read_json(workdir / "meta.json", {}) or {}
    done = read_json(workdir / "uploads.json", {}) or {}
    todo = [c for c in clips if c["id"] not in done and (not only or c["id"] in only)]
    if not todo:
        log("nothing to upload")
        return done

    raw = [c["id"] for c in todo if not c.get("package")]
    if raw:
        log(f"⚠️  not packaged yet: {', '.join(raw)} — run `autoclip package {workdir}` and "
            f"`autoclip render {workdir} --force` first, or they go up with the picker's text")
    y = cfg["youtube"]
    service = get_service(cfg)
    times = _publish_times(len(todo), cfg)
    for clip, publish_at in zip(todo, times):
        path = ready_file(workdir, clip)
        if not path.exists():
            log(f"{path} missing — render first; skipping")
            continue
        status = {"privacyStatus": y["privacy"], "selfDeclaredMadeForKids": bool(y["made_for_kids"])}
        if publish_at:  # scheduled videos must be private until publishAt
            status.update(privacyStatus="private", publishAt=publish_at)
        body = {
            "snippet": {
                "title": _title(clip, cfg),
                "description": _description(clip, meta, cfg),
                "tags": _tags(clip),
                "categoryId": str(y["category_id"]),
            },
            "status": status,
        }
        log(f"uploading {path.name}: {body['snippet']['title']}" + (f"  (publish {publish_at})" if publish_at else ""))
        vid = _upload_one(service, path, body)
        done[clip["id"]] = {"video_id": vid, "url": f"https://youtube.com/shorts/{vid}", "publish_at": publish_at}
        write_json(workdir / "uploads.json", done)
        log(f"  ✓ https://youtube.com/shorts/{vid}")
    return done
