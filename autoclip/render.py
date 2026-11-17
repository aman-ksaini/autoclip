"""Step 4 — cut each clip, reframe to 9:16 (speaker-aware), burn captions."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .captions import build_ass
from .download import source_path
from .reframe import Segment, analyze
from .transcribe import all_words
from .util import ffprobe, fmt_ts, log, read_json, write_json

_SUB_FILTER: str | None | bool = False


def _subtitle_filter() -> str | None:
    """'ass' or 'subtitles' if this ffmpeg has libass, else None (captions are skipped with a warning)."""
    global _SUB_FILTER
    if _SUB_FILTER is False:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        names = {line.split()[1] for line in out.splitlines() if len(line.split()) > 2}
        _SUB_FILTER = "ass" if "ass" in names else "subtitles" if "subtitles" in names else None
        if not _SUB_FILTER:
            log("⚠️  your ffmpeg has no libass — clips will render WITHOUT captions/hook. "
                "Fix on Mac: brew uninstall --ignore-dependencies ffmpeg && "
                "brew tap homebrew-ffmpeg/ffmpeg && brew install homebrew-ffmpeg/ffmpeg/ffmpeg")
    return _SUB_FILTER


def _blur_bg(i: int, W: int, H: int) -> str:
    return (f"[a{i}]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"boxblur=24:2,eq=brightness=-0.08[bg{i}];")


def _layout_filter(seg: Segment, i: int, vw: int, vh: int, cw: int, ch: int, W: int, H: int,
                   zoom: float = 1.0) -> str:
    def clamp(v, lo, hi):
        return int(min(max(v, lo), hi))

    if seg.layout == "crop":
        zw, zh = int(cw / zoom) // 2 * 2, int(ch / zoom) // 2 * 2
        x = clamp(seg.cx - zw / 2, 0, vw - zw)
        y = (vh - ch) // 2 if zoom == 1.0 else clamp(vh * 0.40 - zh / 2, 0, vh - zh)  # punch in towards the face
        return f"crop={zw}:{zh}:{x}:{y},scale={W}:{H}:flags=lanczos,setsar=1[v{i}]"

    if seg.layout == "wide":
        # a wider window that keeps the whole group, shown over a blurred copy
        ww = clamp((seg.x1 - seg.x0) * 1.12, cw, vw) // 2 * 2
        x = clamp((seg.x0 + seg.x1) / 2 - ww / 2, 0, vw - ww)
        return (
            f"split[a{i}][b{i}];" + _blur_bg(i, W, H) +
            f"[b{i}]crop={ww}:{vh // 2 * 2}:{x}:0,scale={W}:-2:flags=lanczos[fg{i}];"
            f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2,setsar=1[v{i}]"
        )

    if seg.layout == "split":
        # two people stacked: each panel W x H/2
        ph = int(vh * 0.8) // 2 * 2
        pw = min(int(ph * W / (H / 2)) // 2 * 2, vw // 2 * 2)
        ph = min(ph, int(pw * (H / 2) / W) // 2 * 2)

        def panel(cx, cy):
            x = clamp(cx - pw / 2, 0, vw - pw)
            y = clamp(cy - ph * 0.42, 0, vh - ph)   # face in the upper-middle of the panel
            return f"crop={pw}:{ph}:{x}:{y},scale={W}:{H // 2}:flags=lanczos"

        return (
            f"split[p{i}][q{i}];"
            f"[p{i}]{panel(seg.cx, seg.cy)}[t{i}];"
            f"[q{i}]{panel(seg.cx2, seg.cy2)}[u{i}];"
            f"[t{i}][u{i}]vstack,setsar=1[v{i}]"
        )

    # fit: full frame over a blurred, zoomed copy of itself
    return (
        f"split[a{i}][b{i}];" + _blur_bg(i, W, H) +
        f"[b{i}]scale={W}:-2:flags=lanczos[fg{i}];"
        f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2,setsar=1[v{i}]"
    )


@dataclass
class Piece:
    a: float              # source time (absolute seconds)
    b: float
    seg: Segment
    zoom: float = 1.0


def _jumpcut_gap(cfg: dict) -> float:
    g = cfg["render"].get("jumpcut_gap", "auto")
    if g in (None, "auto"):
        g = cfg["render"].get("_plan_jumpcut_gap", 0.8)
    return float(g or 0)


def edit_ranges(clip: dict, words: list[dict], reactions: list[dict], cfg: dict) -> list[tuple[float, float]]:
    """Source ranges to play, in order: [teaser] + main clip with dead air removed."""
    s, e = float(clip["start"]), float(clip["end"])
    out: list[tuple[float, float]] = []
    tz = clip.get("teaser")
    if tz and cfg["render"].get("teaser", True):
        out.append((float(tz["start"]), float(tz["end"])))
    gap = _jumpcut_gap(cfg)
    inside = [w for w in words if w["start"] >= s - 0.05 and w["end"] <= e + 0.05]
    cur = s
    if gap > 0:
        for w1, w2 in zip(inside, inside[1:]):
            g0, g1 = w1["end"] + 0.15, w2["start"] - 0.15
            if g1 - g0 < gap - 0.3 or w2["start"] - w1["end"] < gap:
                continue
            if any(r["end"] > w1["end"] and r["start"] < w2["start"] for r in reactions):
                continue  # a laugh/applause lives in this "silence" — keep it
            out.append((cur, g0))
            cur = g1
    out.append((cur, e))
    return [(a, b) for a, b in out if b - a > 0.08]


def build_pieces(ranges, segs: list[Segment], clip_start: float, reactions: list[dict], cfg: dict) -> list[Piece]:
    pieces: list[Piece] = []
    for a, b in ranges:
        for sg in segs:
            pa, pb = max(a, clip_start + sg.start), min(b, clip_start + sg.end)
            if pb - pa > 0.05:
                pieces.append(Piece(pa, pb, sg))
    zoom = float(cfg["render"].get("punch_in", 1.15) or 1.0)
    if zoom <= 1.0:
        return pieces
    out: list[Piece] = []
    for pc in pieces:
        if pc.seg.layout != "crop":
            out.append(pc)
            continue
        hits = [r for r in reactions if r["end"] > pc.a and r["start"] < pc.b]
        if not hits:
            out.append(pc)
            continue
        # one punch-in per reaction: last beat of the punchline -> end of the laugh
        spans = []
        for h in sorted(hits, key=lambda r: r["start"]):
            z0, z1 = max(pc.a, h["start"] - 1.0), min(pc.b, h["end"])
            if spans and z0 <= spans[-1][1] + 0.5:
                spans[-1][1] = max(spans[-1][1], z1)
            else:
                spans.append([z0, z1])
        cur = pc.a
        cuts = []
        for z0, z1 in spans:
            cuts += [(cur, z0, 1.0), (z0, z1, zoom)]
            cur = z1
        cuts.append((cur, pc.b, 1.0))
        for x0, x1, z in cuts:
            if x1 - x0 > 0.35:
                out.append(Piece(x0, x1, pc.seg, z))
            elif out and x1 > x0 and out[-1].b >= x0 - 1e-6:
                out[-1].b = x1  # tiny remainder: extend previous piece
    return out


def map_words(words: list[dict], pieces: list[Piece]) -> tuple[list[dict], float]:
    """Source-timed words -> output-timed words (teaser words appear twice, cut words disappear)."""
    out, t = [], 0.0
    for pc in pieces:
        for w in words:
            if w["end"] > pc.a + 0.02 and w["start"] < pc.b - 0.02:
                out.append({"word": w["word"],
                            "start": t + max(w["start"], pc.a) - pc.a,
                            "end": t + min(w["end"], pc.b) - pc.a})
        t += pc.b - pc.a
    return out, t


def build_graph(pieces: list[Piece], clip_start: float, vw: int, vh: int, cw: int, ch: int,
                W: int, H: int, sub: str | None, has_audio: bool, progress: bool, total: float) -> str:
    n = len(pieces)
    parts = []
    if n > 1:
        parts.append("[0:v]split=" + str(n) + "".join(f"[s{i}]" for i in range(n)))
        if has_audio:
            parts.append("[0:a]asplit=" + str(n) + "".join(f"[r{i}]" for i in range(n)))
    else:
        parts.append("[0:v]null[s0]")
        if has_audio:
            parts.append("[0:a]anull[r0]")
    for i, pc in enumerate(pieces):
        a, b = pc.a - clip_start, pc.b - clip_start
        parts.append(f"[s{i}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS,"
                     + _layout_filter(pc.seg, i, vw, vh, cw, ch, W, H, pc.zoom))
        if has_audio:
            d = b - a
            parts.append(f"[r{i}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,"
                         f"afade=t=in:d=0.015,afade=t=out:st={max(0.0, d - 0.02):.3f}:d=0.02[x{i}]")
    ins = "".join(f"[v{i}]" + (f"[x{i}]" if has_audio else "") for i in range(n))
    parts.append(f"{ins}concat=n={n}:v=1:a={1 if has_audio else 0}[cat]" + ("[aout]" if has_audio else ""))
    last = "cat"
    if progress:
        parts.append(f"color=c=0xFFD400:s={W}x12:r=30[bar];"
                     f"[{last}][bar]overlay=x='-w+w*t/{total:.3f}':y={H - 12}:shortest=1[prog]")
        last = "prog"
    parts.append(f"[{last}]{sub}[vout]" if sub else f"[{last}]null[vout]")
    return ";".join(parts)


def _has_audio(src: Path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "csv=p=0", str(src)], capture_output=True, text=True).stdout
    return bool(out.strip())


def render_clip(src: Path, clip: dict, words: list[dict], cfg: dict, out_dir: Path,
                reactions: list[dict] | None = None) -> Path:
    r = cfg["render"]
    W, H = int(r["width"]), int(r["height"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{clip['id']}.mp4"
    ass = out_dir / f"{clip['id']}.ass"
    reactions = reactions or []

    info = ffprobe(src)
    vw, vh = info["width"], info["height"]
    start, end = float(clip["start"]), float(clip["end"])
    tz = clip.get("teaser")
    if tz:  # the teaser lives inside the clip; make sure the input window covers it
        start, end = min(start, float(tz["start"])), max(end, float(tz["end"]))
    target = W / H
    if vw / vh > target:  # landscape source -> speaker-aware crop
        cw, ch = int(vh * target) // 2 * 2, vh // 2 * 2
        segs = analyze(src, start, end, vw, cw, cfg)
    else:  # already vertical
        cw, ch = vw // 2 * 2, min(int(vw / target) // 2 * 2, vh // 2 * 2)
        segs = [Segment(0.0, end - start, "crop", vw / 2)]

    ranges = edit_ranges(clip, words, reactions, cfg)
    pieces = build_pieces(ranges, segs, start, reactions, cfg)
    out_words, total = map_words(words, pieces)
    build_ass(out_words, total, clip, cfg, ass)
    write_json(out_dir / f"{clip['id']}.edit.json", {
        "layout": [sg.__dict__ for sg in segs],
        "pieces": [{"a": round(pc.a, 2), "b": round(pc.b, 2), "layout": pc.seg.layout, "zoom": pc.zoom} for pc in pieces],
    })
    cuts = len(ranges) - (1 if tz and r.get("teaser", True) else 0) - 1
    log(f"  {len(pieces)} pieces · layouts: {', '.join(sorted({pc.seg.layout for pc in pieces}))}"
        f" · teaser: {'yes' if tz and r.get('teaser', True) else 'no'} · jump cuts: {max(0, cuts)}"
        f" · punch-ins: {sum(1 for pc in pieces if pc.zoom > 1)} · {total:.1f}s")

    sub = None
    sub_name = _subtitle_filter()
    if sub_name:
        sub = f"{sub_name}=filename={ass.name}"
        fonts = Path(r.get("fontsdir") or "")
        if r.get("fontsdir") and fonts.is_dir():
            try:
                rel = os.path.relpath(fonts.resolve(), out_dir.resolve()).replace("\\", "/")
                sub += f":fontsdir={rel}"
            except ValueError:  # different drive on Windows
                pass

    has_audio = _has_audio(src)
    graph = build_graph(pieces, start, vw, vh, cw, ch, W, H, sub, has_audio, bool(r.get("progress_bar")), total)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(src.resolve()),
        "-filter_complex", graph, "-map", "[vout]",
    ]
    if has_audio:
        cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    cmd += ["-c:v", "libx264", "-preset", str(r["preset"]), "-crf", str(r["crf"]), "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", out.name]
    subprocess.run(cmd, cwd=out_dir, check=True)
    return out


def render_all(workdir: Path, cfg: dict, force: bool = False) -> list[Path]:
    workdir = Path(workdir)
    clips = read_json(workdir / "clips.json")
    if not clips:
        raise FileNotFoundError("clips.json missing — run pick first")
    transcript = read_json(workdir / "transcript.json") or {"segments": []}
    words = all_words(transcript)
    src = source_path(workdir)
    out_dir = workdir / "clips"
    plan = read_json(workdir / "plan.json", {}) or {}
    r = cfg["render"]
    if r.get("group_style", "auto") == "auto":
        r["group_style"] = plan.get("group_style", "wide")
    if r.get("reframe", "auto") == "auto":
        r["reframe"] = plan.get("reframe", "speaker")
    r["_plan_jumpcut_gap"] = plan.get("jumpcut_gap", 0.8)
    reactions = (read_json(workdir / "signals.json", {}) or {}).get("reactions", [])
    log(f"framing: {r['reframe']}, groups shown as '{r['group_style']}' ({plan.get('video_type', 'no plan')})"
        f" · jump-cut pauses > {_jumpcut_gap(cfg):.1f}s")
    outs = []
    for c in clips:
        out = out_dir / f"{c['id']}.mp4"
        if out.exists() and not force:
            log(f"{c['id']} already rendered — skipping")
            outs.append(out)
            continue
        log(f"rendering {c['id']}  {fmt_ts(c['start'])} -> {fmt_ts(c['end'])}  “{c.get('title', '')}”")
        outs.append(render_clip(src, c, words, cfg, out_dir, reactions))
    log(f"done — clips are in {out_dir}")
    return outs
