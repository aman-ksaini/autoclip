"""Step 3b — package every clip as YOUR post, not a cut-out.

One AI pass per video reads each clip's transcript and writes, in your channel's voice:
  headline       short title shown at the top of the video for the whole clip
  take           your one-line personal comment, burned into the video
  yt_title       YouTube title written as your own post (no "clip", "episode", "part 3"…)
  yt_description your commentary + a question for viewers
  hashtags       topic hashtags
  slug           file name for the upload (e.g. turned-down-1-crore)

Saved as clip["package"] in clips.json — edit anything there, then `autoclip render <workdir> --force`.
"""
from __future__ import annotations

import re
from pathlib import Path

from .transcribe import all_words
from .util import log, read_json, slug as make_slug, strip_emoji, write_json

SYSTEM = """You run a commentary Shorts channel. You post short moments from other creators' videos and
ADD YOUR OWN VALUE to each one: a clear headline, your personal take, and your own words in the post.
Each post must read like a creator sharing a moment and reacting to it — not like a cut-out of a longer video.

For each moment, using ONLY what is actually said in its transcript:
- headline: on-screen title shown at the top for the whole video. Max 32 characters, no emoji.
  Tells the viewer what this is about (the topic, the situation, the question). Never give away the
  punchline of a joke.
- take: YOUR personal one-line reaction/opinion, shown on screen. Max 70 characters, no emoji, first person.
  Adds a point of view: why it's funny, relatable, surprising or worth thinking about.
- yt_title: YouTube title written as your own post about the moment. Max 70 characters, may use 1 emoji.
  You may name the speaker. NEVER use the words: clip, clipped, part, episode, ep, full video, highlights.
- yt_description: 2-4 short lines in first person: what happens, your take, then a question for viewers
  (if asked). No links, no "clip from", no episode numbers.
- hashtags: 3-6 topic hashtags (strings starting with #).
- slug: 3-6 English words, lowercase, hyphen-separated, describing the topic (used as the file name).

Honesty rules (never break these):
- Do not claim you are the speaker, were present, or made the original video.
- Do not invent facts about the people in it. Opinions are yours; facts must come from the transcript.
- Do not use hate, slurs or anything sexual.

Reply with ONLY a JSON array, no prose, no markdown."""

USER_TMPL = """CHANNEL: {channel}
VOICE: {voice}
WRITE IN: {language}
{question}
VIDEO TOPIC: {summary}  (type: {video_type})
Speakers / show (for context only): {source}

Return one object per moment with keys:
"id", "headline", "take", "yt_title", "yt_description", "hashtags", "slug".

Moments:
{blocks}"""

BANNED = re.compile(
    r"\b(clips?|clipped|full\s+video|highlights?|episode\s*\d*|ep\.?\s*\d+|part\s*\d+|s\d+\s*e\d+)\b",
    re.I,
)


def _clean_title(t: str) -> str:
    t = BANNED.sub("", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" -|:·,")
    return t[:70]


def _clean_on_screen(t: str, n: int) -> str:
    return re.sub(r"\s{2,}", " ", strip_emoji(t or "")).strip()[:n]


def _clip_text(clip: dict, words: list[dict]) -> str:
    s, e = float(clip["start"]), float(clip["end"])
    return " ".join(w["word"].strip() for w in words if w["start"] >= s - 0.05 and w["end"] <= e + 0.05)


def _unique_slug(base: str, used: set[str]) -> str:
    s = make_slug(base or "video", 50) or "video"
    out, i = s, 2
    while out in used:
        out, i = f"{s}-{i}", i + 1
    used.add(out)
    return out


def package_clips(workdir: Path, cfg: dict, force: bool = False) -> list[dict]:
    from .picker import call_llm, parse_json_array

    workdir = Path(workdir)
    clips = read_json(workdir / "clips.json")
    if not clips:
        raise FileNotFoundError("clips.json missing — run pick first")
    if not cfg.get("package", {}).get("enabled", True):
        log("packaging disabled in config — skipping")
        return clips

    todo = [c for c in clips if force or not c.get("package")]
    if not todo:
        log("all clips already packaged — skipping (edit clips.json or use --force)")
        return clips

    transcript = read_json(workdir / "transcript.json") or {"segments": []}
    words = all_words(transcript)
    plan = read_json(workdir / "plan.json", {}) or {}
    meta = read_json(workdir / "meta.json", {}) or {}
    ch = cfg.get("channel", {}) or {}

    blocks = []
    for c in todo:
        text = _clip_text(c, words) or c.get("description", "")
        blocks.append(f'### {c["id"]}\nwhat the picker saw: {c.get("title", "")} — {c.get("reason", "")}\n'
                      f"transcript: {text[:2500]}")

    log(f"packaging {len(todo)} clips: headline, your take, title, description")
    raw = call_llm(cfg, SYSTEM, USER_TMPL.format(
        channel=ch.get("name") or "(your channel)",
        voice=ch.get("voice") or "witty, warm, reacts like a friend sharing a moment",
        language=ch.get("language") or "the same language mix the speakers use",
        question="End every yt_description with one short question for viewers." if ch.get("question", True) else "",
        summary=plan.get("summary") or meta.get("title", ""),
        video_type=plan.get("video_type", "general"),
        source=meta.get("channel") or "",
        blocks="\n\n".join(blocks),
    ))
    got = {str(r.get("id")): r for r in parse_json_array(raw, need_times=False)}

    used = {c["package"]["slug"] for c in clips if c.get("package", {}).get("slug") and c not in todo}
    signoff = (ch.get("signoff") or "").strip()
    for c in todo:
        r = got.get(c["id"])
        if not r:
            log(f"  {c['id']}: AI returned nothing — keeping the picker's text")
            r = {"headline": c.get("hook"), "take": "", "yt_title": c.get("title"),
                 "yt_description": c.get("description"), "hashtags": c.get("hashtags"), "slug": c.get("title")}
        desc = (r.get("yt_description") or "").strip()
        if signoff and signoff not in desc:
            desc = f"{desc}\n\n{signoff}".strip()
        tags = [h if str(h).startswith("#") else f"#{h}" for h in (r.get("hashtags") or c.get("hashtags") or [])]
        c["package"] = {
            "headline": _clean_on_screen(r.get("headline") or c.get("hook") or "", 40),
            "take": _clean_on_screen(r.get("take") or "", 90),
            "yt_title": _clean_title(r.get("yt_title") or c.get("title") or ""),
            "yt_description": desc,
            "hashtags": [re.sub(r"\s+", "", str(t)) for t in tags][:6],
            "slug": _unique_slug(r.get("slug") or r.get("yt_title") or c["id"], used),
        }
        log(f"  {c['id']}: {c['package']['headline']}  |  take: {c['package']['take']}")

    write_json(workdir / "clips.json", clips)
    return clips


def preview(workdir: Path) -> None:
    clips = read_json(Path(workdir) / "clips.json") or []
    for c in clips:
        p = c.get("package") or {}
        print(f"\n  {c['id']}  →  {p.get('slug', '-')}.mp4")
        print(f"    on screen : {p.get('headline', '-')}")
        print(f"    your take : {p.get('take', '-')}")
        print(f"    title     : {p.get('yt_title', '-')}")
        print("    desc      : " + (p.get("yt_description", "-") or "-").replace("\n", "\n                "))
        print(f"    tags      : {' '.join(p.get('hashtags', []))}")
    print()


if __name__ == "__main__":  # quick look: python -m autoclip.package work/<id>
    import sys

    preview(Path(sys.argv[1]))
