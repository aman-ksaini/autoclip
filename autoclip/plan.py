"""Step 2.5 — understand the video and make an editing plan before picking clips.

Inputs: YouTube metadata (title, description, tags, chapters, category), a sample of the transcript,
and whatever you tell it (--type comedy|podcast|gameplay|..., --context "free text").
Output: plan.json — video type, what to clip, what to avoid, 2-3 different "strategies" (lenses)
for finding clips, clip length range and framing style. You can edit plan.json by hand.
"""
from __future__ import annotations

import json
from pathlib import Path

from .util import log, read_json, write_json

# sensible defaults per video type — the LLM plan refines these
PROFILES: dict[str, dict] = {
    "comedy": {
        "focus": "punchlines with the full setup, roasts, crowd work, awkward/absurd moments, big audience laughs",
        "strategies": ["setup -> punchline -> big laugh", "roasts and comebacks between people",
                       "absurd or awkward moments that are funny without context"],
        "min_seconds": 20, "max_seconds": 45, "group_style": "wide", "jumpcut_gap": 1.5,
    },
    "podcast": {
        "focus": "strong opinions, surprising facts, personal stories with a payoff, quotable one-liners, disagreements",
        "strategies": ["hot takes and bold claims", "personal stories with an emotional or surprising payoff",
                       "practical advice / insight in under a minute"],
        "min_seconds": 25, "max_seconds": 60, "group_style": "split", "jumpcut_gap": 0.7,
    },
    "interview": {
        "focus": "revealing answers, emotional moments, surprising confessions, sharp questions with great answers",
        "strategies": ["question -> revealing answer", "emotional or vulnerable moments", "controversial or quotable lines"],
        "min_seconds": 25, "max_seconds": 60, "group_style": "split", "jumpcut_gap": 0.8,
    },
    "gameplay": {
        "focus": "clutch plays, fails, rage/hype reactions, funny bugs, big wins, chat-worthy moments",
        "strategies": ["hype moments and clutch plays", "fails and funny reactions", "skill showcase / tips"],
        "min_seconds": 15, "max_seconds": 40, "group_style": "fit", "jumpcut_gap": 0, "reframe": "fit",
    },
    "educational": {
        "focus": "one clear insight or tip per clip, myth-busting, surprising facts, step-by-step mini lessons",
        "strategies": ["one surprising fact or myth-bust", "actionable tip", "simple explanation of a hard idea"],
        "min_seconds": 30, "max_seconds": 60, "group_style": "wide", "jumpcut_gap": 0.6,
    },
    "vlog": {
        "focus": "funny or emotional moments, reveals, reactions, relatable situations",
        "strategies": ["reveals and reactions", "funny relatable moments", "emotional moments"],
        "min_seconds": 15, "max_seconds": 45, "group_style": "wide", "jumpcut_gap": 0.8,
    },
    "general": {
        "focus": "moments with a strong hook and a clear payoff that work without context",
        "strategies": ["funny moments", "surprising or emotional moments", "quotable insights or opinions"],
        "min_seconds": 20, "max_seconds": 50, "group_style": "wide", "jumpcut_gap": 1.0,
    },
}

PLAN_SYSTEM = """You are the head of a short-form content team. Before editors cut a long video into
Shorts/Reels, you study it and write a precise editing plan. Be specific to THIS video
(name the people, recurring bits, best segments) — not generic advice. Reply with ONLY JSON."""

PLAN_TMPL = """Make an editing plan for cutting this video into vertical short clips.

What the user told you: type = {user_type!r}; context = {user_context!r}
Known video types: {types}

VIDEO METADATA
{meta}

TRANSCRIPT SAMPLE (beginning, middle, end)
{sample}

Return a JSON object with keys:
"video_type"   one of the known types,
"summary"      2 sentences: what this video is,
"people"       array of main people and their role, if identifiable,
"audience"     who will watch the clips,
"focus"        what makes a great clip from THIS video (1-3 sentences),
"avoid"        what to skip (1 sentence),
"strategies"   array of 2-3 different lenses for finding clips, each a short sentence,
"min_seconds", "max_seconds"   clip length range that suits this content,
"group_style"  "wide" (show everyone in one shot), "split" (two people stacked) or "fit" (whole frame),
"jumpcut_gap"  remove silences longer than this many seconds (comedy needs its pauses: 1.5; podcasts 0.7; 0 = never),
"notes"        anything else the editors should know."""


def rich_meta(workdir: Path) -> dict:
    """meta.json with description/tags/chapters/heatmap (fetched once from YouTube if missing)."""
    workdir = Path(workdir)
    meta = read_json(workdir / "meta.json", {}) or {}
    url = meta.get("url") or ""
    if meta.get("_rich") or "youtu" not in url:
        return meta
    try:
        from yt_dlp import YoutubeDL

        with YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        meta.update({
            "title": info.get("title") or meta.get("title"),
            "channel": info.get("channel") or info.get("uploader"),
            "description": (info.get("description") or "")[:3000],
            "tags": (info.get("tags") or [])[:30],
            "categories": info.get("categories") or [],
            "chapters": [{"start": c.get("start_time"), "title": c.get("title")} for c in (info.get("chapters") or [])],
            "view_count": info.get("view_count"),
            "heatmap": [
                {"start": round(h["start_time"], 2), "end": round(h["end_time"], 2), "value": round(h["value"], 3)}
                for h in (info.get("heatmap") or [])
            ],
            "_rich": True,
        })
        write_json(workdir / "meta.json", meta)
    except Exception as e:
        log(f"could not fetch video details ({e.__class__.__name__})")
    return meta


def _sample(transcript: dict, chars: int = 9000) -> str:
    segs = transcript.get("segments", [])
    if not segs:
        return ""
    lines = [f"[{s['start']:.0f}s] {s['text']}" for s in segs]
    part = chars // 3
    out, n = [], len(lines)
    for start in (0, n // 2, max(0, n - 60)):
        chunk, size = [], 0
        for line in lines[start:]:
            if size + len(line) > part:
                break
            chunk.append(line)
            size += len(line)
        out.append("\n".join(chunk))
    return "\n...\n".join(out)


def make_plan(workdir: Path, transcript: dict, cfg: dict, force: bool = False) -> dict:
    from .picker import call_llm

    workdir = Path(workdir)
    out = workdir / "plan.json"
    p = cfg["pick"]
    if out.exists() and not force:
        plan = read_json(out)
        log(f"plan exists ({plan.get('video_type')}) — skipping (edit plan.json or use --force)")
        return plan

    user_type = (p.get("video_type") or "").lower().strip()
    base = PROFILES.get(user_type, PROFILES["general"])
    meta = rich_meta(workdir)
    meta_txt = json.dumps(
        {k: meta.get(k) for k in ("title", "channel", "description", "tags", "categories", "chapters", "duration")},
        ensure_ascii=False, indent=1,
    )
    plan = {"video_type": user_type or "general", **base}
    if p.get("plan", True):
        log("planning: studying the video to decide what to clip")
        try:
            from .picker import parse_json_object

            raw = call_llm(cfg, PLAN_SYSTEM, PLAN_TMPL.format(
                user_type=user_type or "unknown", user_context=p.get("context") or "",
                types=", ".join(PROFILES), meta=meta_txt[:6000], sample=_sample(transcript),
            ))
            llm = parse_json_object(raw)
            vt = str(llm.get("video_type") or user_type or "general").lower()
            if user_type:  # the user's word wins
                vt = user_type
            plan = {**PROFILES.get(vt, PROFILES["general"]), **{k: v for k, v in llm.items() if v not in (None, "", [])}}
            plan["video_type"] = vt
        except Exception as e:
            log(f"planning failed ({e}) — using the '{plan['video_type']}' preset")
    plan.setdefault("audience", p.get("audience"))
    write_json(out, plan)
    _print_plan(plan)
    return plan


def _print_plan(plan: dict) -> None:
    print("\n  ── editing plan ─────────────────────────────")
    print(f"  type:      {plan.get('video_type')}")
    if plan.get("summary"):
        print(f"  about:     {plan['summary']}")
    print(f"  focus:     {plan.get('focus')}")
    for i, s in enumerate(plan.get("strategies", []), 1):
        print(f"  lens {i}:    {s}")
    print(f"  length:    {plan.get('min_seconds')}–{plan.get('max_seconds')}s   framing for groups: {plan.get('group_style')}")
    print("  ─────────────────────────────────────────────\n")
