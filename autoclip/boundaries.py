"""Clean clip edges so a clip never starts or ends in the wrong place.

Rules (deterministic, run after the AI picks):
  * never start mid-sentence  -> move start back (or forward) to a sentence start
  * never end mid-sentence    -> extend to the sentence end (or pull back to the previous one)
  * orphan head: 1-4 words, then a long pause = the tail of the previous bit -> drop them
  * orphan tail: a long pause, then 1-4 words = the start of the next bit     -> drop them
  * laugh tail: a reaction right after the punchline is part of the clip       -> keep it
"""
from __future__ import annotations

import re

END_PUNCT = re.compile(r"[.?!।…]+[\"')\]]*$")
SENTENCE_GAP = 0.45      # a pause this long counts as a sentence break (captions often lack punctuation)
ORPHAN_GAP = 0.8
ORPHAN_WORDS = 4


def _is_end(w: dict) -> bool:
    return bool(END_PUNCT.search(w["word"]))


def sentence_starts(words: list[dict]) -> list[int]:
    out = [0] if words else []
    for i in range(1, len(words)):
        if _is_end(words[i - 1]) or words[i]["start"] - words[i - 1]["end"] >= SENTENCE_GAP:
            out.append(i)
    return out


def _sentence_ends(words: list[dict]) -> list[int]:
    starts = sentence_starts(words)
    return [s - 1 for s in starts[1:]] + ([len(words) - 1] if words else [])


def fix_boundaries(clip: dict, words: list[dict], reactions: list[dict],
                   min_s: float, max_s: float, total: float) -> dict:
    """Return clip with cleaned start/end (and a 'boundary_notes' list explaining changes)."""
    if not words:
        return clip
    notes: list[str] = []
    s, e = float(clip["start"]), float(clip["end"])
    starts = sentence_starts(words)
    ends = _sentence_ends(words)

    # index of first/last word inside the clip
    inside = [i for i, w in enumerate(words) if w["end"] > s + 0.05 and w["start"] < e - 0.05]
    if not inside:
        return clip
    i0, i1 = inside[0], inside[-1]

    # --- orphan head: the clip opens on a few words followed by a long pause
    #     (= the tail of the previous bit) -> start at the next sentence instead
    nxt = [k for k in starts if k > i0]
    if nxt and nxt[0] - i0 <= ORPHAN_WORDS and nxt[0] < i1:
        gap = words[nxt[0]]["start"] - words[nxt[0] - 1]["end"]
        if gap >= ORPHAN_GAP:
            i0 = nxt[0]
            notes.append("dropped the tail of the previous bit at the start")

    # --- start: on a sentence start
    if i0 not in starts:
        back = [k for k in starts if k < i0 and words[i0]["start"] - words[k]["start"] <= 6]
        fwd = [k for k in starts if k > i0 and words[k]["start"] - words[i0]["start"] <= 5 and k < i1]
        if back and words[i1]["end"] - words[back[-1]]["start"] <= max_s + 2:
            i0 = back[-1]
            notes.append("start moved back to the beginning of the sentence")
        elif fwd:
            i0 = fwd[0]
            notes.append("start moved forward to the next sentence")

    # --- end: on a sentence end
    if i1 not in ends:
        fwd = [k for k in ends if k > i1 and words[k]["end"] - words[i0]["start"] <= max_s + 2]
        back = [k for k in ends if i0 < k < i1]
        if fwd and words[fwd[0]]["end"] - words[i1]["end"] <= 5:
            i1 = fwd[0]
            notes.append("end extended to finish the sentence")
        elif back:
            i1 = back[-1]
            notes.append("end pulled back to the previous full sentence")

    # --- orphan tail: long pause, then a few words = the next bit starting
    last_start = max([k for k in starts if k <= i1] or [i0])
    if last_start > i0 and i1 - last_start + 1 <= ORPHAN_WORDS:
        gap = words[last_start]["start"] - words[last_start - 1]["end"]
        if gap >= ORPHAN_GAP and words[last_start - 1]["end"] - words[i0]["start"] >= min_s * 0.8:
            i1 = last_start - 1
            notes.append("dropped the start of the next bit at the end")

    start = max(0.0, words[i0]["start"] - 0.12)
    end = words[i1]["end"] + 0.35

    # --- laugh tail: keep the reaction that follows the payoff
    after = [r for r in reactions if end - 0.5 <= r["start"] <= end + 1.5]
    if after:
        r_end = max(r["end"] for r in after)
        nxt_word = next((w for w in words[i1 + 1:]), None)
        limit = min(r_end + 0.3, end + 4.0, start + max_s + 4.0)
        if nxt_word is not None:
            limit = min(limit, max(end, nxt_word["start"] - 0.05))  # but don't swallow the next line
        if limit > end:
            end = limit
            notes.append("kept the audience reaction after the punchline")

    clip["start"], clip["end"] = round(start, 2), round(min(end, total or end), 2)
    if notes:
        clip["boundary_notes"] = notes
    return clip
