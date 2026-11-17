"""Adds all new settings to your existing config.yaml (keeps your own choices). Safe to run twice."""
import re

p = "config.yaml"
s = open(p).read()

# --- update 1: no fixed clip count, plan-based lengths
if "max_clips" not in s:
    s = re.sub(r"  count: 6[^\n]*\n  min_seconds: 25\n  max_seconds: 35\n", '''  video_type: ""             # comedy | podcast | interview | gameplay | educational | vlog | general  ("" = let the AI decide; or --type)
  context: ""                # anything that helps, e.g. "Samay Raina's roast show, finale episode" (or --context)
  plan: true                 # AI studies the video first and writes plan.json (focus, 2-3 approaches, length, framing)
  count: null                # null = keep EVERY clip that scores >= min_score; a number = exactly that many
  min_score: 70              # quality bar (critic score 1-100 + bonuses)
  min_clips: 3
  max_clips: 15
  min_seconds: null          # null = use the plan's length for this type of video
  max_seconds: null
  max_strategies: 3          # how many different approaches to try
''', s, count=1)
s = s.replace("  candidates_factor: 2       # pass 1 proposes count x this, pass 2 (critic) keeps the best\n", "")

# --- update 1: group framing
if "group_style" not in s:
    s = re.sub(r"  reframe: (?:speaker|face)[^\n]*\n(?:  multi_speaker: [^\n]*\n)?", '''  reframe: auto              # auto (from plan) | speaker = follow whoever is talking | face | center | fit = full frame + blur
  multi_speaker: auto        # 2+ people far apart: auto = cut to the speaker, but keep everyone during a bit/back-and-forth
                             #   switch = always cut to the speaker | wide | split | fit = always show the group
  group_style: auto          # how a group is shown: auto (from plan) | wide (everyone, blurred bars) | split (2 people stacked) | fit
''', s, count=1)

# --- update 2: boundary review
if "boundary_review" not in s:
    s = re.sub(r"(  refine: [^\n]*\n)", r'''\1  boundary_review: true      # third AI pass: fix start/end (no mid-sentence, no next-bit tails, keep the laugh) + teaser + keywords
''', s, count=1)

# --- update 2: viral edit elements
if "punch_in" not in s:
    s = re.sub(r"(  hook: [^\n]*\n)", r'''\1  headline: intro            # intro = hook box for the first seconds | always = headline bar for the whole clip | off
  teaser: true               # cold open: flash the strongest 2-4s line first, then play the clip (AI decides per clip)
  punch_in: 1.15             # zoom in on punchlines / reactions (1.0 = off)
  jumpcut_gap: auto          # remove silences longer than N seconds (auto = from plan: comedy 1.5, podcast 0.7; 0 = off)
  keyword_color: "&H0000FF7F&"   # colour for the AI-chosen keywords in captions (green)
  progress_bar: false        # thin progress bar at the bottom
''', s, count=1)

open(p, "w").write(s)
checks = ["max_clips", "group_style", "boundary_review", "punch_in"]
missing = [c for c in checks if c not in s]
print("config.yaml updated ✓" if not missing else f"config.yaml: could not add {missing} — add them from README")
