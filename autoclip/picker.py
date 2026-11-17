"""Step 3 — pick the best short-form moments.

  plan (plan.json)  ->  pass 1: one search per strategy/lens  ->  pass 2: critic scores + tightens
  ->  final score = critic verdict + YouTube most-replayed + audience reactions
  ->  keep every clip above pick.min_score (between min_clips and max_clips)
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import requests

from .transcribe import all_words
from .util import log, read_json, write_json

SYSTEM = """You are a senior short-form video editor who has grown channels to millions of views.
You read a timestamped transcript of a long video and find moments that will perform as standalone
vertical clips (YouTube Shorts / Instagram Reels).

A great clip:
- HOOK: the first 2 seconds grab attention (surprising line, question, bold claim, conflict, setup)
- STANDALONE: makes sense with zero context from the rest of the video
- PAYOFF: punchline, reveal, emotional peak or genuinely useful insight — and ENDS right after it
  (keep the laugh/reaction, never cut mid-sentence, never start mid-sentence)
- one idea per clip, no slow intro, no "as I said earlier"

Signals in the transcript (real data — use them):
- {HOT:NN}  = YouTube "most replayed" score 0-100 for that moment (viewers rewatched it)
- {REACT}   = audience reaction detected in the audio (laughter, applause, shouting)
- [..] tags like [laughter] / [हंसी] / [प्रशंसा] = reactions written in the captions
A setup followed by {REACT} is usually a punchline worth clipping. High {HOT} is strong evidence.

Rules:
- Use ONLY timestamps that exist in the transcript (seconds, decimals allowed).
- Only propose moments that are genuinely strong. Fewer great clips beat many average ones.
- Titles/hooks/descriptions in the language mix the audience uses (Hinglish is fine).
- "hook" is on-screen text for the first seconds: max 40 characters, no emoji.
- Reply with ONLY a JSON array, no prose, no markdown."""

USER_TMPL = """VIDEO: {summary}
Type: {video_type}. Audience: {audience}
What makes a great clip from this video: {focus}
Avoid: {avoid}
{extra}
YOUR LENS FOR THIS PASS: {lens}
Find every strong moment through this lens (up to {n}), each {min_s}-{max_s} seconds long.

Return a JSON array of objects with exactly these keys:
"start" (number, seconds), "end" (number, seconds),
"title" (YouTube title, max 70 chars, may include 1 emoji),
"hook" (on-screen text, max 40 chars, no emoji),
"description" (1-2 lines),
"hashtags" (array of 3-6 strings starting with #),
"score" (1-100 predicted virality),
"reason" (one short line).

Transcript ({span}), format [start-end] text:
{transcript}"""

REFINE_SYSTEM = """You are a ruthless short-form video editor doing the final cut.
You get candidate clips (found by different editors using different approaches) with the exact
transcript around them. For each one:
1. Judge it honestly: HOOK (do the first 2 seconds stop the scroll?), STANDALONE (clear without context?),
   PAYOFF (is there a real punchline/insight/emotion at the end?). Most candidates are average — say so.
   Score on an absolute scale: 80+ = would genuinely go viral, 60-79 = solid, below 60 = skip.
2. Tighten the cut: start exactly where the hook begins (drop warm-up words), end right after the
   payoff and the reaction. Stay within the allowed length. Use only timestamps from the excerpt.
3. If two candidates are the same moment, give the weaker one total=0.
4. Improve title and hook text if you can.
Reply with ONLY a JSON array, no prose, no markdown."""

REFINE_TMPL = """VIDEO: {summary}  (type: {video_type}; audience: {audience})
What makes a great clip here: {focus}
Allowed length: {min_s}-{max_s} seconds.

For EVERY candidate return an object with keys:
"id", "start", "end", "hook_score" (1-10), "standalone_score" (1-10), "payoff_score" (1-10),
"total" (1-100, your final verdict), "title", "hook", "description", "hashtags", "reason".

Candidates:
{blocks}"""


# ---------------------------------------------------------------- LLM providers
BOUNDARY_SYSTEM = """You are the finishing editor. Each clip below is already chosen; your job is the EDGES
and the opening. For every clip, read the transcript around it and fix:
- START: must be the beginning of the thought/bit. Not mid-sentence, not the last line of the previous
  topic, not a slow warm-up ("so", "and then", "umm", greetings). If the first lines are weak, start later.
- END: right after the payoff (punchline, answer, reveal) and the reaction to it. Never end mid-sentence,
  never end on the first words of the next topic/bit, never cut the laugh.
- TEASER (cold open): if the strongest 2-4 second line is NOT at the start, give its start/end so it can be
  shown first as a flash-forward ("wait for it"). Use null if the clip already opens with its hook.
  Never use the final punchline as the teaser for a joke (it spoils it) — use a strong reaction or a
  provocative line instead.
- KEYWORDS: 3-6 words from the clip's speech to highlight in the captions (names, numbers, the punch word).
Use only timestamps from the transcript. Reply with ONLY a JSON array, no prose."""

BOUNDARY_TMPL = """Allowed clip length: {min_s}-{max_s} seconds (a laugh at the end may add a few seconds).

Return for EVERY clip: "id", "start", "end", "teaser" ({{"start": n, "end": n}} or null),
"keywords" (array of words), "edge_note" (what you changed, short).

{blocks}"""


def _post(url: str, headers: dict, payload: dict, retries: int = 4) -> dict:
    for attempt in range(retries):
        r = requests.post(url, headers=headers, json=payload, timeout=300)
        if r.status_code in (429, 500, 502, 503, 529) and attempt < retries - 1:
            wait = 2 ** attempt * 5
            log(f"LLM API {r.status_code}, retrying in {wait}s")
            time.sleep(wait)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"LLM API error {r.status_code}: {r.text[:500]}")
        return r.json()
    raise RuntimeError("LLM API: retries exhausted")


def _need(key: str) -> str:
    v = os.environ.get(key)
    if not v:
        raise RuntimeError(f"{key} is not set — add it to .env")
    return v


def call_llm(cfg: dict, system: str, user: str) -> str:
    p = cfg["pick"]
    provider = p["provider"].lower()
    if provider in ("claude-code", "claude_code", "cli"):
        # uses your Claude Pro/Max subscription through the Claude Code CLI — no API key needed
        import shutil
        import subprocess

        exe = shutil.which("claude")
        if not exe:
            raise RuntimeError("Claude Code CLI not found — install it: npm install -g @anthropic-ai/claude-code, then run `claude` once to log in")
        cmd = [exe, "-p", "--output-format", "text", "--system-prompt", system]
        if p.get("cli_model"):  # e.g. sonnet / opus
            cmd += ["--model", str(p["cli_model"])]
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # force subscription login
        r = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=900, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"claude CLI failed: {r.stderr.strip()[:500] or r.stdout[:500]}")
        return r.stdout
    if provider == "anthropic":
        data = _post(
            "https://api.anthropic.com/v1/messages",
            {
                "x-api-key": _need("ANTHROPIC_API_KEY"),
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            {
                "model": p["model"],
                "max_tokens": 4096,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
        return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
    if provider == "gemini":
        data = _post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{p['model']}:generateContent",
            {"x-goog-api-key": _need("GEMINI_API_KEY"), "content-type": "application/json"},
            {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"},
            },
        )
        return "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"])
    if provider == "openai":
        base = (p.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        key = os.environ.get("OPENAI_API_KEY", "none")  # Ollama needs no key
        data = _post(
            f"{base}/chat/completions",
            {"authorization": f"Bearer {key}", "content-type": "application/json"},
            {
                "model": p["model"],
                "temperature": 0.4,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            },
        )
        return data["choices"][0]["message"]["content"]
    raise ValueError(f"unknown pick.provider: {provider}")


# ---------------------------------------------------------------- parsing & cleanup
def parse_json_array(text: str, need_times: bool = True) -> list[dict]:
    text = re.sub(r"```(?:json)?", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            raise ValueError(f"LLM did not return JSON: {text[:300]}")
        data = json.loads(m.group(0))
    if isinstance(data, dict):  # e.g. {"clips": [...]}
        data = next((v for v in data.values() if isinstance(v, list)), [])
    return [c for c in data if isinstance(c, dict) and (not need_times or ("start" in c and "end" in c))]


def _windows(segments: list[dict], max_chars: int) -> list[list[dict]]:
    """Split long transcripts into windows the LLM can read in one go (with a small overlap)."""
    windows, cur, size = [], [], 0
    for seg in segments:
        line_len = len(seg["text"]) + 20
        if cur and size + line_len > max_chars:
            windows.append(cur)
            cur = cur[-8:]  # overlap so a moment on the boundary isn't lost
            size = sum(len(s["text"]) + 20 for s in cur)
        cur.append(seg)
        size += line_len
    if cur:
        windows.append(cur)
    return windows


def _overlap(a: dict, b: dict) -> float:
    """Seconds two clips share."""
    return max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))


def snap_to_words(clip: dict, words: list[dict], min_s: float, max_s: float, total: float) -> dict | None:
    """Move start/end onto real word boundaries and enforce the length limits."""
    if not words:
        clip["start"] = max(0.0, float(clip["start"]))
        clip["end"] = min(total, float(clip["end"]))
        return clip if clip["end"] - clip["start"] >= min_s * 0.6 else None

    s, e = float(clip["start"]), float(clip["end"])
    first = min(words, key=lambda w: abs(w["start"] - s))
    start = max(0.0, first["start"] - 0.15)

    inside = [w for w in words if w["start"] >= first["start"]]
    # last word that ends close to the requested end, within max length
    candidates = [w for w in inside if w["end"] - start <= max_s - 0.4]
    if not candidates:
        return None
    last = min(candidates, key=lambda w: abs(w["end"] - e))
    # too short? extend to the first word end that reaches min length
    if last["end"] - start < min_s:
        longer = [w for w in candidates if w["end"] - start >= min_s]
        if longer:
            last = longer[0]
    end = min(total, last["end"] + 0.4)  # breathing room for the reaction
    if end - start < min_s * 0.6:
        return None
    clip["start"], clip["end"] = round(start, 2), round(end, 2)
    return clip


def parse_json_object(text: str) -> dict:
    text = re.sub(r"```(?:json)?", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError(f"LLM did not return JSON: {text[:300]}")
        data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def _annotate(seg: dict, sig: dict) -> str:
    from .signals import heat_at, reactions_in

    marks = ""
    h = heat_at(sig, seg["start"], seg["end"])
    if h >= 0.45:
        marks += f" {{HOT:{int(h * 100)}}}"
    if any(not r.get("tag") for r in reactions_in(sig, seg["start"], seg["end"] + 1.5)):
        marks += " {REACT}"
    return f"[{seg['start']:.1f}-{seg['end']:.1f}]{marks} {seg['text']}"


def _excerpt(segments: list[dict], sig: dict, t0: float, t1: float) -> str:
    return "\n".join(_annotate(s, sig) for s in segments if s["end"] > t0 and s["start"] < t1)


def _ctx(plan: dict, p: dict) -> dict:
    return {
        "summary": plan.get("summary") or "(unknown)",
        "video_type": plan.get("video_type", "general"),
        "audience": plan.get("audience") or p.get("audience"),
        "focus": plan.get("focus") or "strong hook and clear payoff",
        "avoid": "; ".join(filter(None, [plan.get("avoid"), p.get("avoid")])),
    }


def _dedupe(cands: list[dict], limit: int) -> list[dict]:
    """Drop near-duplicates (same moment found by two lenses), keep the higher score."""
    out: list[dict] = []
    for c in sorted(cands, key=lambda c: -float(c.get("score", 0))):
        try:
            c["start"], c["end"] = float(c["start"]), float(c["end"])
        except (TypeError, ValueError):
            continue
        shorter = lambda o: min(c["end"] - c["start"], o["end"] - o["start"]) or 1  # noqa: E731
        if any(_overlap(c, o) / shorter(o) > 0.6 for o in out):
            continue
        out.append(c)
    return out[:limit]


def _refine(candidates: list[dict], transcript: dict, sig: dict, cfg: dict, plan: dict, lens: tuple) -> list[dict]:
    p = cfg["pick"]
    blocks = []
    for i, c in enumerate(candidates):
        c["id"] = f"c{i + 1}"
        ex = _excerpt(transcript["segments"], sig, c["start"] - 12, c["end"] + 12)
        blocks.append(f"### {c['id']}  proposed {c['start']:.1f}-{c['end']:.1f}  “{c.get('title', '')}”\n{ex}")
    log(f"pass 2: critic reviews {len(candidates)} candidates")
    user = REFINE_TMPL.format(**_ctx(plan, p), min_s=lens[0], max_s=lens[1], blocks="\n\n".join(blocks))
    try:
        refined = parse_json_array(call_llm(cfg, REFINE_SYSTEM, user), need_times=False)
    except Exception as e:
        log(f"critic pass failed ({e}) — using first-pass scores")
        return candidates
    by_id = {c["id"]: c for c in candidates}
    out = []
    for r in refined:
        base = by_id.get(str(r.get("id")))
        if not base:
            continue
        merged = {**base, **{k: v for k, v in r.items() if v not in (None, "", [])}}
        merged["score"] = float(r.get("total", base.get("score", 0)))
        out.append(merged)
    return out or candidates


def _boundary_review(clips: list[dict], transcript: dict, sig: dict, cfg: dict, min_s: float, max_s: float) -> list[dict]:
    blocks = []
    for c in clips:
        lines = []
        for seg in transcript["segments"]:
            if seg["end"] <= c["start"] - 15 or seg["start"] >= c["end"] + 15:
                continue
            where = "IN " if seg["end"] > c["start"] + 0.2 and seg["start"] < c["end"] - 0.2 else "out"
            lines.append(f"{where} {_annotate(seg, sig)}")
        blocks.append(f"### {c['id']}  current {c['start']:.1f}-{c['end']:.1f}  “{c.get('title', '')}”\n" + "\n".join(lines))
    log(f"pass 3: finishing editor checks start/end of {len(clips)} clips")
    try:
        res = parse_json_array(call_llm(cfg, BOUNDARY_SYSTEM, BOUNDARY_TMPL.format(
            min_s=int(min_s), max_s=int(max_s), blocks="\n\n".join(blocks))), need_times=False)
    except Exception as e:
        log(f"boundary review failed ({e}) — keeping rule-based edges only")
        return clips
    by_id = {c["id"]: c for c in clips}
    for r in res:
        c = by_id.get(str(r.get("id")))
        if not c:
            continue
        try:
            ns, ne = float(r.get("start", c["start"])), float(r.get("end", c["end"]))
            if 0.5 * min_s <= ne - ns <= max_s + 5 and abs(ns - c["start"]) < 20 and abs(ne - c["end"]) < 20:
                c["start"], c["end"] = ns, ne
        except (TypeError, ValueError):
            pass
        tz = r.get("teaser")
        if isinstance(tz, dict):
            try:
                c["teaser"] = {"start": float(tz["start"]), "end": float(tz["end"])}
            except (KeyError, TypeError, ValueError):
                pass
        if isinstance(r.get("keywords"), list):
            c["keywords"] = [str(k) for k in r["keywords"]][:8]
        if r.get("edge_note"):
            c["edge_note"] = r["edge_note"]
    return clips


def _clean_teaser(c: dict, words: list[dict]) -> None:
    tz = c.get("teaser")
    if not tz:
        c.pop("teaser", None)
        return
    inside = [w for w in words if w["start"] >= tz["start"] - 0.3 and w["end"] <= tz["end"] + 0.3
              and w["start"] >= c["start"] and w["end"] <= c["end"]]
    if not inside:
        c.pop("teaser", None)
        return
    t0, t1 = inside[0]["start"] - 0.1, inside[-1]["end"] + 0.15
    if not (1.0 <= t1 - t0 <= 4.5) or t0 < c["start"] + 2.0:
        c.pop("teaser", None)  # too long/short, or already at the start -> pointless
        return
    c["teaser"] = {"start": round(t0, 2), "end": round(t1, 2)}


def pick_clips(workdir: Path, cfg: dict, force: bool = False) -> list[dict]:
    from .signals import get_signals, heat_at, reactions_in

    workdir = Path(workdir)
    out = workdir / "clips.json"
    if out.exists() and not force:
        log("clips.json exists — skipping (edit it by hand or use --force)")
        return read_json(out)

    transcript = read_json(workdir / "transcript.json")
    if not transcript:
        raise FileNotFoundError("transcript.json missing — run transcribe first")
    p = cfg["pick"]
    plan = read_json(workdir / "plan.json", {}) or {}
    # lengths: CLI/config override > plan > defaults
    min_s = float(p.get("min_seconds") or plan.get("min_seconds") or 20)
    max_s = float(p.get("max_seconds") or plan.get("max_seconds") or 45)
    max_clips = int(p.get("count") or p.get("max_clips", 15))
    min_clips = int(p.get("count") or p.get("min_clips", 3))
    min_score = 0.0 if p.get("count") else float(p.get("min_score", 70))
    sig = get_signals(workdir, transcript, cfg)
    ctx = _ctx(plan, p)

    # pass 1 — one search per lens (different editing approaches)
    lenses = (plan.get("strategies") or ["the strongest moments overall"])[: int(p.get("max_strategies", 3))]
    windows = _windows(transcript["segments"], int(p.get("window_chars", 60000)))
    extra = f"Extra instructions from the creator: {p['instructions']}" if p.get("instructions") else ""
    per_call = max(4, round(max_clips * 1.2 / len(windows)))
    candidates: list[dict] = []
    for li, lens in enumerate(lenses, 1):
        for wi, win in enumerate(windows, 1):
            span = f"{win[0]['start']:.0f}s to {win[-1]['end']:.0f}s"
            log(f"pass 1 · lens {li}/{len(lenses)} “{lens}” · part {wi}/{len(windows)}")
            user = USER_TMPL.format(
                **ctx, extra=extra, lens=lens, n=per_call, min_s=int(min_s), max_s=int(max_s), span=span,
                transcript="\n".join(_annotate(s, sig) for s in win),
            )
            try:
                found = parse_json_array(call_llm(cfg, SYSTEM, user))
            except Exception as e:
                log(f"  lens failed ({e}) — continuing")
                continue
            for c in found:
                c["lens"] = lens
            candidates += found
    candidates = _dedupe(candidates, int(p.get("max_candidates", 30)))
    log(f"pass 1: {len(candidates)} unique candidates from {len(lenses)} approaches")
    if not candidates:
        raise RuntimeError("no candidates found — check the LLM provider")

    # pass 2 — critic
    if p.get("refine", True):
        candidates = _refine(candidates, transcript, sig, cfg, plan, (int(min_s), int(max_s)))

    # final ranking = critic verdict + real-world signals
    words = all_words(transcript)
    total = float(transcript.get("duration") or (words[-1]["end"] if words else 0))
    scored = []
    for c in candidates:
        c = snap_to_words(dict(c), words, min_s, max_s, total)
        if not c:
            continue
        heat = heat_at(sig, c["start"], c["end"])
        reacts = len(reactions_in(sig, c["start"], c["end"]))
        c["signals"] = {"most_replayed": round(heat, 2), "reactions": reacts}
        c["final_score"] = round(float(c.get("score", 0)) + heat * 20 + min(reacts, 3) * 2, 1)
        scored.append(c)

    chosen: list[dict] = []
    for c in sorted(scored, key=lambda c: -c["final_score"]):
        if len(chosen) >= max_clips:
            break
        if c["final_score"] < min_score and len(chosen) >= min_clips:
            break
        if any(_overlap(c, o) > 1.0 for o in chosen):
            continue
        chosen.append(c)

    chosen.sort(key=lambda c: c["start"])
    for i, c in enumerate(chosen, 1):
        c["id"] = f"clip_{i:02d}"
        c.setdefault("hashtags", [])

    # pass 3 — clean edges: AI finishing editor, then hard rules
    from .boundaries import fix_boundaries

    if p.get("boundary_review", True) and chosen:
        chosen = _boundary_review(chosen, transcript, sig, cfg, min_s, max_s)
    reacts_all = sig.get("reactions", [])
    for c in chosen:
        fix_boundaries(c, words, reacts_all, min_s, max_s, total)
        _clean_teaser(c, words)
    write_json(out, chosen)
    log(f"picked {len(chosen)} clips (score ≥ {min_score:.0f}, max {max_clips}) -> {out}")
    return chosen
