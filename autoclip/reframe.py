"""Speaker-aware 9:16 reframing.

For each clip we sample ~5 frames/sec and:
  1. detect faces (OpenCV YuNet if available, Haar cascade fallback)
  2. detect camera cuts (colour-histogram change)
  3. measure mouth movement per face  ->  who is talking
  4. build a timeline of segments, each with a layout:
       crop  — 9:16 window centred on the active speaker (or on a group that fits)
       wide  — wider window that keeps everyone in a bit/conversation, over a blurred background
       split — two people stacked top/bottom (podcasts, interviews, back-and-forth)
       fit   — whole frame, letterboxed over a blurred copy (no face / wide shot / can't tell)
The renderer turns that timeline into one ffmpeg filtergraph (hard cuts between segments,
like a human editor would do — no seasick panning).
"""
from __future__ import annotations

import statistics
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .util import log

YUNET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
    "face_detection_yunet_2023mar.onnx"
)


@dataclass
class Segment:
    start: float          # seconds, relative to clip start
    end: float
    layout: str           # "crop" | "wide" | "split" | "fit"
    cx: float = 0.0       # crop: centre x of the 9:16 window; split: top person
    x0: float = 0.0       # wide: left edge of the group
    x1: float = 0.0       # wide: right edge of the group
    cy: float = 0.0       # split: top person's face centre y
    cx2: float = 0.0      # split: bottom person
    cy2: float = 0.0


@dataclass
class Face:
    cx: float
    cy: float
    w: float
    h: float
    box_small: tuple      # x, y, w, h in the downscaled frame (for mouth crops)
    motion: float = 0.0
    track: int = -1


@dataclass
class Sample:
    t: float
    faces: list[Face] = field(default_factory=list)
    shot: int = 0


# ------------------------------------------------------------------ face detector
_DETECTOR = None


def _get_detector(cfg: dict):
    global _DETECTOR
    if _DETECTOR is not None:
        return _DETECTOR
    import cv2

    model = Path(cfg["render"].get("models_dir", "models")) / "face_detection_yunet_2023mar.onnx"
    if not model.exists() and cfg["render"].get("download_models", True):
        try:
            model.parent.mkdir(parents=True, exist_ok=True)
            log("downloading YuNet face model (230 KB, one time)")
            urllib.request.urlretrieve(YUNET_URL, model)
        except Exception as e:
            log(f"YuNet download failed ({e.__class__.__name__}) — using Haar cascade")
    if model.exists() and model.stat().st_size > 100_000 and hasattr(cv2, "FaceDetectorYN"):
        det = cv2.FaceDetectorYN.create(str(model), "", (320, 320), 0.7, 0.3, 50)

        def detect(img):
            det.setInputSize((img.shape[1], img.shape[0]))
            _, faces = det.detect(img)
            return [] if faces is None else [tuple(map(float, f[:4])) for f in faces]

        _DETECTOR = (detect, "yunet")
        return _DETECTOR

    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    def detect(img):
        if cascade.empty():
            return []
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        found = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(24, 24))
        return [tuple(map(float, f)) for f in found]

    _DETECTOR = (detect, "haar")
    return _DETECTOR


# ------------------------------------------------------------------ sampling
def _sample(src: Path, start: float, end: float, vw: int, cfg: dict) -> list[Sample]:
    import cv2
    import numpy as np

    detect, _ = _get_detector(cfg)
    sfps = float(cfg["render"].get("analysis_fps", 5))
    cap = cv2.VideoCapture(str(src))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
    t0 = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000 or start
    step = max(1, round(fps / sfps))
    small_w = 480
    scale = small_w / vw
    min_face = float(cfg["render"].get("min_face_ratio", 0.045)) * vw

    samples: list[Sample] = []
    prev_hist = None
    prev_gray = None
    prev_faces: list[Face] = []
    shot = 0
    i = 0
    while True:
        if not cap.grab():
            break
        t = t0 + i / fps
        if t > end:
            break
        if t >= start and i % step == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
            hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
            cv2.normalize(hist, hist)
            if prev_hist is not None and cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL) < 0.55:
                shot += 1
                prev_faces = []
            prev_hist = hist

            faces = []
            for x, y, w, h in detect(small):
                if w / scale < min_face:
                    continue  # audience / background people
                faces.append(Face(cx=(x + w / 2) / scale, cy=(y + h / 2) / scale, w=w / scale, h=h / scale,
                                  box_small=(x, y, w, h)))
            # mouth movement vs the same face in the previous sample
            for f in faces:
                g = min(prev_faces, key=lambda p: abs(p.cx - f.cx) + abs(p.cy - f.cy), default=None)
                if g is None or abs(g.cx - f.cx) > f.w * 0.6 or prev_gray is None:
                    continue
                f.motion = _mouth_diff(gray, prev_gray, f.box_small, g.box_small, np, cv2)
            samples.append(Sample(t=t - start, faces=faces, shot=shot))
            prev_gray, prev_faces = gray, faces
        i += 1
    cap.release()
    return samples


def _mouth_diff(gray, prev_gray, box, prev_box, np, cv2) -> float:
    def patch(img, b):
        x, y, w, h = b
        x0, x1 = int(x + 0.2 * w), int(x + 0.8 * w)
        y0, y1 = int(y + 0.62 * h), int(y + 0.98 * h)
        H, W = img.shape[:2]
        x0, x1, y0, y1 = max(0, x0), min(W, x1), max(0, y0), min(H, y1)
        if x1 - x0 < 4 or y1 - y0 < 3:
            return None
        return cv2.resize(img[y0:y1, x0:x1], (24, 12)).astype(np.float32)

    a, b = patch(gray, box), patch(prev_gray, prev_box)
    if a is None or b is None:
        return 0.0
    # remove global brightness change, keep shape change (mouth opening/closing)
    a -= a.mean()
    b -= b.mean()
    return float(np.abs(a - b).mean())


# ------------------------------------------------------------------ timeline
def _assign_tracks(samples: list[Sample], vw: int) -> None:
    """Group faces into people per shot by horizontal position."""
    tracks: list[dict] = []
    for s in samples:
        for f in s.faces:
            best = None
            for tr in tracks:
                if tr["shot"] == s.shot and abs(tr["cx"] - f.cx) < max(0.06 * vw, f.w * 0.7):
                    if best is None or abs(tr["cx"] - f.cx) < abs(best["cx"] - f.cx):
                        best = tr
            if best is None:
                best = {"id": len(tracks), "shot": s.shot, "cx": f.cx}
                tracks.append(best)
            best["cx"] = 0.7 * best["cx"] + 0.3 * f.cx
            f.track = best["id"]


def build_timeline(samples: list[Sample], duration: float, vw: int, crop_w: int, cfg: dict) -> list[Segment]:
    r = cfg["render"]
    min_hold = float(r.get("min_shot_seconds", 1.2))
    window = 1.0
    _assign_tracks(samples, vw)

    raw: list[Segment] = []
    shots: dict[int, list[Sample]] = {}
    for s in samples:
        shots.setdefault(s.shot, []).append(s)

    for shot_samples in shots.values():
        s0 = shot_samples[0].t
        s1 = shot_samples[-1].t + (1 / float(r.get("analysis_fps", 5)))
        with_faces = [s for s in shot_samples if s.faces]
        if len(with_faces) < 0.4 * len(shot_samples):
            raw.append(Segment(s0, s1, "fit"))
            continue

        # people present in this shot (seen in >= 30% of its samples)
        counts: dict[int, list[Face]] = {}
        for s in with_faces:
            for f in s.faces:
                counts.setdefault(f.track, []).append(f)
        people = {k: v for k, v in counts.items() if len(v) >= 0.3 * len(shot_samples)} or counts
        centres = {k: statistics.median(f.cx for f in v) for k, v in people.items()}
        widths = {k: statistics.median(f.w for f in v) for k, v in people.items()}

        if len(people) == 1:
            k = next(iter(people))
            raw.append(Segment(s0, s1, "crop", centres[k]))
            continue

        left = min(centres[k] - widths[k] / 2 for k in people)
        right = max(centres[k] + widths[k] / 2 for k in people)
        if right - left <= crop_w * 0.92:  # everyone fits in one 9:16 window
            raw.append(Segment(s0, s1, "crop", (left + right) / 2))
            continue

        mode = r.get("multi_speaker", "auto")
        style = r.get("group_style", "wide")
        cys = {k: statistics.median(f.cy for f in v) for k, v in people.items()}
        activity = {k: sum(f.motion for f in v) for k, v in people.items()}
        group_seg = lambda a, b: _group_segment(a, b, style, people, centres, widths, cys, activity, vw)  # noqa: E731
        if mode in ("wide", "split", "fit"):
            style = mode
            raw.append(group_seg(s0, s1))
            continue

        # who talks in each 1s window (most mouth movement), and is it a duet?
        labels: list[tuple[float, float, object]] = []
        t = s0
        while t < s1:
            win = [s for s in shot_samples if t <= s.t < t + window]
            motion: dict[int, float] = {}
            for s in win:
                for f in s.faces:
                    if f.track in people:
                        motion[f.track] = motion.get(f.track, 0.0) + f.motion
            ranked = sorted(motion.items(), key=lambda kv: -kv[1])
            label: object = None
            if ranked and ranked[0][1] > 0:
                top, val = ranked[0]
                second = ranked[1][1] if len(ranked) > 1 else 0.0
                if mode == "auto" and second >= 0.6 * val:
                    label = "GROUP"            # two mouths moving together: a bit / argument / duet
                elif val >= second * 1.3:
                    label = top
            labels.append((t, min(t + window, s1), label))
            t += window

        # fill unknown windows with the previous decision
        filled, prev = [], None
        for a0, a1, lab in labels:
            lab = lab if lab is not None else prev
            filled.append([a0, a1, lab])
            prev = lab

        # back-and-forth: 2+ different speakers within ~4s -> keep everyone in frame
        if mode == "auto":
            spk = [lab for _, _, lab in filled]
            for i in range(len(filled)):
                around = [x for x in spk[max(0, i - 2): i + 3] if x is not None]
                switches = sum(1 for x, y in zip(around, around[1:]) if x != y)
                if len(set(around)) >= 2 and switches >= 2:
                    filled[i][2] = "GROUP"

        for a0, a1, lab in filled:
            if lab is None:
                raw.append(Segment(a0, a1, "fit"))
            elif lab == "GROUP":
                raw.append(group_seg(a0, a1))
            else:
                raw.append(Segment(a0, a1, "crop", centres[lab]))

    return _clean(raw, duration, vw, min_hold)


def _group_segment(a0, a1, style, people, centres, widths, cys, activity, vw) -> Segment:
    if style == "split" and len(people) >= 2:
        two = sorted(sorted(people, key=lambda k: -activity.get(k, 0))[:2], key=lambda k: centres[k])
        top, bot = two
        return Segment(a0, a1, "split", cx=centres[top], cy=cys[top], cx2=centres[bot], cy2=cys[bot])
    if style == "fit":
        return Segment(a0, a1, "fit")
    left = min(centres[k] - widths[k] for k in people)
    right = max(centres[k] + widths[k] for k in people)
    if right - left > 0.9 * vw:
        return Segment(a0, a1, "fit")
    return Segment(a0, a1, "wide", x0=max(0.0, left), x1=min(float(vw), right))


def _same(a: Segment, b: Segment, vw: int) -> bool:
    if a.layout != b.layout:
        return False
    tol = 0.05 * vw
    if a.layout == "crop":
        return abs(a.cx - b.cx) < tol
    if a.layout == "wide":
        return abs(a.x0 - b.x0) < tol and abs(a.x1 - b.x1) < tol
    if a.layout == "split":
        return abs(a.cx - b.cx) < tol and abs(a.cx2 - b.cx2) < tol
    return True


def _clean(segs: list[Segment], duration: float, vw: int, min_hold: float) -> list[Segment]:
    if not segs:
        return [Segment(0.0, duration, "fit")]
    segs = sorted(segs, key=lambda s: s.start)
    # merge neighbours that look the same
    merged = [segs[0]]
    for s in segs[1:]:
        m = merged[-1]
        if _same(m, s, vw):
            m.end = s.end
        else:
            merged.append(s)
    # absorb very short segments into the previous one (avoid flicker)
    out: list[Segment] = []
    for s in merged:
        if out and (s.end - s.start) < min_hold:
            out[-1].end = s.end
        else:
            out.append(s)
    if len(out) > 1 and out[0].end - out[0].start < min_hold:
        out[1].start = out[0].start
        out.pop(0)
    # make it gap-free and cover the whole clip
    out[0].start = 0.0
    for a, b in zip(out, out[1:]):
        a.end = b.start
    out[-1].end = duration
    # a final merge pass after absorption
    final = [out[0]]
    for s in out[1:]:
        m = final[-1]
        if _same(m, s, vw):
            m.end = s.end
        else:
            final.append(s)
    return final[:40]


def analyze(src: Path, start: float, end: float, vw: int, crop_w: int, cfg: dict) -> list[Segment]:
    mode = cfg["render"].get("reframe", "speaker")
    if mode == "auto":
        mode = "speaker"
    dur = end - start
    if mode == "fit":
        return [Segment(0.0, dur, "fit")]
    if mode == "center":
        return [Segment(0.0, dur, "crop", vw / 2)]
    try:
        samples = _sample(src, start, end, vw, cfg)
    except Exception as e:
        log(f"reframe analysis failed ({e}) — centre crop")
        return [Segment(0.0, dur, "crop", vw / 2)]
    if mode == "face":  # legacy: one static crop on the most common face
        xs = [f.cx for s in samples for f in s.faces]
        return [Segment(0.0, dur, "crop", statistics.median(xs) if xs else vw / 2)]
    return build_timeline(samples, dur, vw, crop_w, cfg)
