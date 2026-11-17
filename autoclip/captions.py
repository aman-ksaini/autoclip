"""Build an .ass subtitle file: word-by-word captions (current word + keywords highlighted)
and a headline/hook box at the top."""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

from .util import strip_emoji


def _t(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _norm(w: str) -> str:
    return re.sub(r"[^\w]+", "", w.lower(), flags=re.UNICODE)


def _chunks(words: list[dict], per_line: int) -> list[list[dict]]:
    out, cur = [], []
    for w in words:
        if cur and (
            len(cur) >= per_line
            or w["start"] - cur[-1]["end"] > 0.6          # pause -> new caption
            or cur[-1]["word"][-1:] in ".?!,।"            # sentence/phrase end
        ):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def drop_tags(words: list[dict]) -> list[dict]:
    """Remove caption tags like [हंसी], [संगीत], [हौसला बढ़ाने की आवाज़], (laughs), ♪ from on-screen captions.
    A tag can span several words, so skip everything from an opening bracket to its closing one."""
    pairs = {"[": "]", "(": ")"}
    out, close, skipped = [], None, 0
    for w in words:
        t = w["word"].strip()
        if close:                       # inside a multi-word tag (max 8 words, in case it never closes)
            skipped += 1
            if close in t or skipped >= 8:
                close = None
            continue
        if t[:1] in pairs:              # a tag starts here
            if pairs[t[0]] not in t:
                close, skipped = pairs[t[0]], 0
            continue
        t = re.sub(r"\[[^\]]*\]|\([^)]*\)|[♪♫\[\]()]", "", t).strip()
        if t:
            out.append({**w, "word": t})
    return out


def build_ass(words: list[dict], duration: float, clip: dict, cfg: dict, out_path: Path) -> Path:
    """words: [{start, end, word}] with times RELATIVE TO THE OUTPUT clip."""
    r = cfg["render"]
    W, H = int(r["width"]), int(r["height"])
    font = r["caption_font"]
    hl = r["highlight_color"]
    kw_color = r.get("keyword_color", "&H0000FF7F&")
    keywords = {_norm(k) for k in clip.get("keywords", []) if _norm(k)}

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{r['caption_size']},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,2,60,60,{r['caption_margin_v']},1
Style: Hook,{font},{r['hook_size']},&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,18,0,8,80,80,230,1
Style: Take,{font},{r.get('take_size', 54)},&H00FFFFFF,&H00FFFFFF,&H40000000,&H40000000,-1,-1,0,0,100,100,0,0,3,16,0,2,90,90,{r.get('take_margin_v', 760)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []

    # headline / hook box at the top (a packaged clip uses its headline)
    pkg = clip.get("package") or {}
    mode = r.get("headline", "intro")  # intro | always | off
    hook = strip_emoji(pkg.get("headline") or clip.get("hook") or clip.get("title") or "")
    if r.get("hook", True) and hook and mode != "off":
        wrapped = r"\N".join(textwrap.wrap(_esc(hook), width=22)[:3])
        hs = duration if mode == "always" else min(float(r["hook_seconds"]), duration)
        events.append(f"Dialogue: 1,{_t(0)},{_t(hs)},Hook,,0,0,0,,{{\\fad(150,200)}}{wrapped}")

    # your take: one personal line, shown above the captions
    take = strip_emoji(pkg.get("take") or "")
    when = str(r.get("take_when", "end")).lower()  # start | end | always | off
    if take and when != "off":
        secs = min(float(r.get("take_seconds", 4)), duration)
        t0, t1 = {"start": (0.0, secs), "always": (0.0, duration)}.get(when, (max(0.0, duration - secs), duration))
        label = (r.get("take_label") or "").strip()
        text = r"\N".join(textwrap.wrap(_esc(take), width=30)[:3])
        if label:
            text = f"{{\\fs{int(r.get('take_size', 54) * 0.7)}\\c&H0000FFFF&}}{_esc(label)}{{\\r}}\\N{text}"
        events.append(f"Dialogue: 2,{_t(t0)},{_t(t1)},Take,,0,0,0,,{{\\fad(200,150)}}{text}")

    # word-by-word captions
    if r.get("captions"):
        upper = bool(r.get("caption_uppercase"))
        chunks = _chunks(drop_tags(sorted(words, key=lambda w: w["start"])), int(r["words_per_line"]))
        for ci, chunk in enumerate(chunks):
            nxt = chunks[ci + 1][0]["start"] if ci + 1 < len(chunks) else duration
            chunk_end = min(nxt, chunk[-1]["end"] + 0.35)
            texts = [_esc(w["word"].upper() if upper else w["word"]) for w in chunk]
            is_kw = [_norm(w["word"]) in keywords for w in chunk]
            for wi, w in enumerate(chunk):
                ws = w["start"]
                we = chunk[wi + 1]["start"] if wi + 1 < len(chunk) else chunk_end
                if we <= ws:
                    continue
                parts = []
                for j, t in enumerate(texts):
                    if j == wi:
                        parts.append(f"{{\\c{hl}\\fscx112\\fscy112}}{t}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}")
                    elif is_kw[j]:
                        parts.append(f"{{\\c{kw_color}}}{t}{{\\c&H00FFFFFF&}}")
                    else:
                        parts.append(t)
                events.append(f"Dialogue: 0,{_t(ws)},{_t(we)},Caption,,0,0,0,,{' '.join(parts)}")

    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return out_path
