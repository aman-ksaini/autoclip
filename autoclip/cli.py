"""autoclip — long video -> AI-picked vertical clips -> YouTube Shorts.

  autoclip run <url-or-file> [--upload]     full pipeline
  autoclip batch urls.txt [--upload]        one URL per line
  autoclip plan|pick|package|render <workdir>  re-run one step (e.g. after editing plan.json / clips.json)
  autoclip preview [workdir]                what will be uploaded: headline, your take, title, description
  autoclip upload [workdir]                 upload (no workdir = the most recent video)

  content hints:  --type comedy|podcast|interview|gameplay|educational|vlog  --context "..."
                  --instructions "..."  --max-clips 15  --min-score 70
  autoclip auth                             one-time YouTube sign-in
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_config
from .util import fmt_ts, log, read_json


def _summary(workdir: Path) -> None:
    clips = read_json(workdir / "clips.json") or []
    print("\n  #  time             score  title")
    for c in clips:
        sc = c.get("final_score", c.get("score", ""))
        print(f"  {c['id'][-2:]} {fmt_ts(c['start']):>6} → {fmt_ts(c['end']):<6}  {sc:>5}  {c.get('title', '')}")
    print(f"\n  clips:   {workdir / 'clips'}\n  edit:    {workdir / 'clips.json'}  then `autoclip render {workdir} --force`\n")


def _workdir_or_latest(workdir: str | None, cfg: dict) -> Path:
    if workdir:
        return Path(workdir)
    root = Path(cfg.get("workdir", "work"))
    dirs = [d for d in root.iterdir() if (d / "clips.json").exists()] if root.is_dir() else []
    if not dirs:
        raise SystemExit("no videos in work/ yet — run `autoclip run <link>` first")
    return max(dirs, key=lambda d: (d / "clips.json").stat().st_mtime)


def run_one(source: str, cfg: dict, upload: bool, force: bool) -> Path:
    from .download import fetch
    from .picker import pick_clips
    from .render import render_all
    from .transcribe import transcribe

    workdir = fetch(source, cfg)
    log(f"workdir: {workdir}")
    transcribe(workdir, cfg, force=force)
    from .plan import make_plan

    make_plan(workdir, read_json(workdir / "transcript.json"), cfg, force=force)
    pick_clips(workdir, cfg, force=force)
    from .package import package_clips

    package_clips(workdir, cfg, force=force)
    render_all(workdir, cfg, force=force)
    _summary(workdir)
    if upload:
        from .youtube import upload_all

        upload_all(workdir, cfg)
    return workdir


def _content_args(sp) -> None:
    sp.add_argument("--type", dest="video_type", help="comedy | podcast | interview | gameplay | educational | vlog | general")
    sp.add_argument("--context", help='what the video is, e.g. "stand-up roast show with guests"')
    sp.add_argument("--instructions", help='what to look for, e.g. "roast moments, emotional stories"')
    sp.add_argument("--max-clips", type=int, dest="max_clips")
    sp.add_argument("--min-score", type=float, dest="min_score")


def _apply_content_args(a, cfg: dict) -> None:
    for k in ("video_type", "context", "instructions", "max_clips", "min_score"):
        v = getattr(a, k, None)
        if v not in (None, ""):
            cfg["pick"][k] = v


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="autoclip", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="download → transcribe → pick → render [→ upload]")
    p.add_argument("source", help="YouTube (or any yt-dlp) URL, or a local video file")
    p.add_argument("--upload", action="store_true")
    p.add_argument("--count", type=int)
    p.add_argument("--min", type=float, dest="min_s")
    p.add_argument("--max", type=float, dest="max_s")
    _content_args(p)
    p.add_argument("--force", action="store_true", help="redo steps even if outputs exist")

    b = sub.add_parser("batch", help="run for every URL/file in a text file")
    b.add_argument("listfile")
    b.add_argument("--upload", action="store_true")

    for name in ("transcribe", "plan", "pick", "package", "render"):
        s = sub.add_parser(name)
        s.add_argument("workdir")
        s.add_argument("--force", action="store_true")
        if name in ("plan", "pick"):
            _content_args(s)
    pv = sub.add_parser("preview", help="show headline, your take, title, description per clip")
    pv.add_argument("workdir", nargs="?")
    u = sub.add_parser("upload", help="upload rendered clips of a workdir (default: the most recent one)")
    u.add_argument("workdir", nargs="?")
    u.add_argument("--only", nargs="*", help="clip ids, e.g. clip_01 clip_03")
    sub.add_parser("auth", help="sign in to YouTube once and save token.json")

    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    _apply_content_args(a, cfg)

    try:
        if a.cmd == "run":
            if a.count:
                cfg["pick"]["count"] = a.count
            if a.min_s:
                cfg["pick"]["min_seconds"] = a.min_s
            if a.max_s:
                cfg["pick"]["max_seconds"] = a.max_s
            run_one(a.source, cfg, a.upload, a.force)
        elif a.cmd == "batch":
            items = [l.strip() for l in Path(a.listfile).read_text(encoding="utf-8").splitlines()]
            for src in [i for i in items if i and not i.startswith("#")]:
                try:
                    run_one(src, cfg, a.upload, False)
                except Exception as e:  # keep going with the next video
                    log(f"FAILED {src}: {e}")
        elif a.cmd == "transcribe":
            from .transcribe import transcribe

            transcribe(Path(a.workdir), cfg, force=a.force)
        elif a.cmd == "plan":
            from .plan import make_plan

            make_plan(Path(a.workdir), read_json(Path(a.workdir) / "transcript.json"), cfg, force=a.force)
        elif a.cmd == "pick":
            from .picker import pick_clips
            from .plan import make_plan

            wd = Path(a.workdir)
            # a new --type/--context means the old plan is stale
            make_plan(wd, read_json(wd / "transcript.json"), cfg,
                      force=a.force and bool(getattr(a, "video_type", None) or getattr(a, "context", None)))
            pick_clips(wd, cfg, force=a.force)
            _summary(Path(a.workdir))
        elif a.cmd == "package":
            from .package import package_clips, preview

            package_clips(Path(a.workdir), cfg, force=a.force)
            preview(Path(a.workdir))
            log(f"now re-render so the headline and your take are burned in: autoclip render {a.workdir} --force")
        elif a.cmd == "preview":
            from .package import preview

            preview(_workdir_or_latest(a.workdir, cfg))
        elif a.cmd == "render":
            from .render import render_all

            render_all(Path(a.workdir), cfg, force=a.force)
        elif a.cmd == "upload":
            from .youtube import upload_all

            wd = _workdir_or_latest(a.workdir, cfg)
            log(f"uploading from {wd}")
            upload_all(wd, cfg, only=a.only)
        elif a.cmd == "auth":
            from .youtube import get_service

            get_service(cfg)
            log("YouTube sign-in saved to token.json")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
