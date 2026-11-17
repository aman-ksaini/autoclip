# autoclip 🎬

> **Personal project.** autoclip is used only by its author to upload to his own YouTube channel (Light Moments).
> It is not a public service and has no other users. Each Short adds the author's own headline and commentary and
> credits the original creator. See [PRIVACY.md](PRIVACY.md) and the diagrams in [docs/](docs/).

**🔒 [Privacy Policy](PRIVACY.md) · 📄 [Terms of Service](TERMS.md) · ▶️ Uses YouTube API Services — [YouTube Terms of Service](https://www.youtube.com/t/terms) · [Google Privacy Policy](https://policies.google.com/privacy)**

**Long video → AI-picked 30s vertical clips with captions → YouTube Shorts.**
A free, self-hosted OpusClip-style pipeline.

```
YouTube link / file
  → yt-dlp            download
  → faster-whisper    transcript with word timestamps (local, free)
  → LLM               picks the best 25–35s moments + title, hook, hashtags
  → OpenCV + FFmpeg   9:16 crop around the speaker, word-by-word captions, hook title
  → YouTube Data API  upload as Shorts (private / scheduled)
```

---

## 1. Install (10 min)

**Requirements:** Python 3.10+, FFmpeg on PATH.

| OS | FFmpeg |
|---|---|
| Windows | `winget install Gyan.FFmpeg` |
| macOS | `brew install ffmpeg` |
| Linux | `sudo apt install ffmpeg` |

```bash
cd autoclip
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e .
cp .env.example .env        # then paste your API key
```

**LLM key (pick one):** put it in `.env` and set `pick.provider` / `pick.model` in `config.yaml`.

| Provider | Cost | `.env` key | config |
|---|---|---|---|
| **Claude Code CLI** (your Pro/Max plan) | ₹0 extra | – | `provider: claude-code` (install Claude Code, run `claude` once to log in) |
| Anthropic API | ~₹2–5 per video | `ANTHROPIC_API_KEY` | `provider: anthropic` |
| Google Gemini | free tier | `GEMINI_API_KEY` | `provider: gemini`, `model:` a current Gemini model |
| Groq / OpenRouter | free tier / cheap | `OPENAI_API_KEY` | `provider: openai`, `base_url:` their URL |
| Ollama (local) | ₹0 | – | `provider: openai`, `base_url: http://localhost:11434/v1` |

Hindi captions use the bundled **Noto Sans Devanagari** font in `fonts/` (OFL licence).

---

## 2. Make clips

```bash
autoclip run "https://www.youtube.com/watch?v=XXXX"
# or a local file
autoclip run "D:/videos/podcast.mp4" --count 8 --min 25 --max 35
```

Output in `work/<video-id>/`:

```
source.mp4          the downloaded video
transcript.json     words + timestamps
clips.json          the AI's picks (title, hook, description, hashtags, score)
clips/clip_01.mp4   ready-to-post 1080×1920 clips
```

**Review loop:** don't like a pick? Edit `start`/`end`/`title`/`hook` in `clips.json`, then

```bash
autoclip render work/<video-id> --force
```

Every step caches its output, so re-runs are instant. Use `--force` to redo.

**First run** downloads the Whisper model (`small` ≈ 460 MB). On CPU, a 50-min video takes roughly 10–20 min with `small`.
Use `model: base` for speed or `medium` / `large-v3` for accuracy (a GPU helps a lot).

---

## 3. YouTube upload (one-time setup, 15 min)

1. Go to <https://console.cloud.google.com> → create a project.
2. **APIs & Services → Library →** enable **YouTube Data API v3**.
3. **OAuth consent screen →** External → fill the app name and your email → add **your Gmail as a Test user**.
4. **Credentials → Create credentials → OAuth client ID → Desktop app** → download the JSON → save it as `client_secret.json` in this folder.
5. Run once:

```bash
autoclip auth      # browser opens → pick your channel → token.json is saved
```

Then:

```bash
autoclip upload work/<video-id>                 # all clips
autoclip upload work/<video-id> --only clip_01  # just one
autoclip run "<url>" --upload                   # whole pipeline, hands-off
```

Uploaded IDs are saved in `uploads.json`, so a clip is never uploaded twice.

**Scheduling** (one clip per day at 7 PM IST):

```yaml
youtube:
  schedule:
    enabled: true
    start: "2026-11-17T19:00:00+05:30"
    every_hours: 24
```

### ⚠️ The one catch: the API audit

YouTube forces every video uploaded by an **unaudited** API project to **private**. They can't be made public later, even from YouTube Studio.

- Quota is not the problem: new projects get **100 uploads a day**.
- **Apply for the audit on day 1:** *YouTube API Services → Audit and Quota Extension form*. It's free and can take a few weeks.
- Until it's approved, either use `autoclip run` without `--upload` and post the `clips/*.mp4` files manually in YouTube Studio, or use a posting service that has already passed the audit.

---

## 4. Automate it

`urls.txt` holds one link per line (lines starting with `#` are ignored):

```bash
autoclip batch urls.txt --upload
```

Schedule it with **Task Scheduler** (Windows) or **cron** (macOS/Linux):

```cron
0 9 * * *  cd /path/to/autoclip && .venv/bin/autoclip batch urls.txt --upload >> autoclip.log 2>&1
```

---

## How it decides *what* to clip and *where* to look

### 1. Plan (`plan.json`)
Before picking, the AI studies the video's title, description, tags and chapters, plus parts of the transcript and anything you tell it:

```bash
autoclip run "<url>" --type comedy --context "Samay Raina roast show, finale with old contestants"
```

It writes a plan with:
- the video type
- what makes a great clip from **this** video
- what to avoid
- **2–3 different approaches** (e.g. "setup → punchline → big laugh", "roasts between people", "emotional moments")
- the right clip length
- how to frame groups

Types are comedy, podcast, interview, gameplay, educational, vlog and general. Each starts from a sensible preset.
Edit `plan.json` by hand if you like, then run `autoclip pick <workdir> --force`.

### 2. Pick
1. **Signals:** YouTube "Most replayed" data and audience reactions (laughter/applause spikes, `[हंसी]` tags) are marked in the transcript.
2. **Pass 1:** one search **per approach**. Each one finds every strong moment through its own lens. Duplicates are merged.
3. **Pass 2, the critic:** re-reads every candidate with context, scores the hook, whether it stands alone and the payoff, tightens the cut and kills duplicates.
4. **No fixed number:** every clip with a final score of at least `min_score` (70) is kept, between `min_clips` (3) and `max_clips` (15). Use `--count 6` to force exactly 6.

### 3. Clean edges (pass 3 + hard rules)
A **finishing editor** pass re-reads 15 seconds around every clip and fixes the edges. Hard rules then guarantee:
- **No mid-sentence starts or ends.** The start moves back to the beginning of the sentence, and the end extends to finish it.
- **No tail of the previous bit at the start**, i.e. a few words followed by a long pause.
- **No first words of the next bit at the end.**
- **The laugh is kept** when an audience reaction follows the punchline.

Every change is recorded in `clips.json` under `boundary_notes` and `edge_note`.

### 4. Frame (speaker-aware 9:16)
About 5 frames per second are analysed for faces, camera cuts and **mouth movement**:

| Situation | Layout |
|---|---|
| One person talking | **crop**: 9:16 centred on them |
| Several people close together | **crop** centred on the group |
| One person talking at length while others listen | **crop** on the speaker |
| A **bit**, an argument, quick back-and-forth, or two people talking or laughing at once | **group**: `wide` (everyone in one shot over a blurred background) or `split` (two people stacked) |
| Nobody on screen, or a wide or audience shot | **fit**: full frame over a blurred background |

The group style comes from the plan (comedy uses `wide`, podcasts and interviews use `split`, gameplay uses `fit`), or set `render.group_style` yourself.
Each clip's decisions are saved in `clips/clip_XX.layout.json`.

## Viral edit elements (on by default)

| Element | What it does | Why it matters |
|---|---|---|
| **Cold-open teaser** | Flashes the strongest 2–4 seconds first, then plays the clip | Most viewers decide in the first 2–3 seconds |
| **Jump cuts** | Removes dead air (comedy keeps pauses up to 1.5s for timing; podcasts cut above 0.7s). Never cuts a laugh | Pacing = retention |
| **Punch-in zoom** | Zooms 15% on the last beat of a punchline and through the laugh | Pattern interrupt; makes the payoff land |
| **Word-by-word captions** | Current word in yellow, **AI-picked keywords in green** | Most people scroll with the sound off |
| **Hook / headline box** | `headline: intro` (first 3s) or `always` (a title bar for the whole clip, like clip channels use) | Context instantly |
| **Progress bar** | `progress_bar: true` | Optional; some niches like it |

Each clip's edit decisions are saved in `clips/clip_XX.edit.json`.

## Tips that matter more than editing
- **Watch "Viewed vs swiped away"** in YouTube Studio → Analytics → Advanced. It's the first gate for Shorts. If many people swipe in the first 3 seconds, fix the hook or teaser.
- **Engineer loops.** An ending that flows back into the start gets rewatches, which count as views.
- **Quality over volume.** A long episode usually has a handful of truly strong clips. Raise `min_score` if you're posting average ones.
- **Stick to 2–3 sources or shows** so the channel builds a clear audience.
- **A/B test openings:** post the same clip with two different first 3 seconds (`teaser: false` vs `true`, or two hooks) and keep what wins.
- **Rights:** clip your own content, or shows that allow clipping (many run paid clipping programs). Add commentary or context, because pure re-uploads risk "reused content" demonetization and Content ID claims.

## Config cheat sheet (`config.yaml`)

| Key | What it does |
|---|---|
| `transcribe.language` | `hi`, `en`, or `null` for auto-detect |
| `transcribe.task` | `translate` gives English captions for a Hindi video |
| `pick.count / min_seconds / max_seconds` | how many clips and how long |
| `pick.audience / avoid` | steers the AI's taste and keeps clips Shorts-safe |
| `render.reframe` | `face` (follows the speaker) or `center` |
| `render.words_per_line`, `caption_size`, `highlight_color` | caption style |
| `render.hook` | on-screen hook text for the first 3 seconds |
| `youtube.privacy` | `private` / `unlisted` / `public` |
| `youtube.credit` | source credit added to the description |

## Project layout

```
autoclip/
  cli.py         commands (run, batch, pick, render, upload, auth)
  download.py    yt-dlp / local files
  transcribe.py  faster-whisper → transcript.json
  picker.py      LLM prompt, JSON parsing, snapping to word boundaries, de-overlap
  captions.py    .ass karaoke captions + hook title
  render.py      face-centred 9:16 crop + FFmpeg encode
  youtube.py     OAuth, resumable upload, scheduling
```

## Known limits / ideas for v2

- Face crop is **static per clip** (median face position). Dynamic tracking or split-screen for two speakers would be a nice upgrade.
- Emoji are stripped from burned-in text, because libass can't draw colour emoji. They still appear in YouTube titles.
- Next ideas: Instagram/TikTok upload, B-roll, a web dashboard to approve clips, a virality score from real view data.

## ⚖️ Use responsibly

Only clip videos **you own or have permission to use**. Re-uploading other creators' content leads to Content ID claims and channel strikes, and YouTube can demonetize channels for "reused content". Downloading other people's videos may also break YouTube's Terms of Service.

## Post it as yours: headline + your take (`autoclip package`)

Between picking and rendering, autoclip writes everything in your channel's voice (set `channel:` in config.yaml):

| Field | Where it shows |
|---|---|
| `headline` | top of the video, for the whole clip |
| `take` | your 1-line personal comment, above the captions (last 4 s by default, `render.take_when`) |
| `yt_title` | YouTube title (words like clip / episode / part are removed) |
| `yt_description` | your commentary + a question for viewers |
| `hashtags` | topic hashtags |
| `slug` | file name, e.g. `sarkar-wali-shayari.mp4` (in `work/<id>/ready/`) |

```bash
autoclip run "<link>"                     # now includes packaging
autoclip preview                          # check what will be posted (latest video)
# edit clip["package"] in clips.json if you want, then:
autoclip render work/<id> --force
autoclip upload                           # uploads the latest video's clips
autoclip upload --only clip_02 clip_05    # just these
```

Old video, made before this update? `autoclip package work/<id>` then `autoclip render work/<id> --force`.

The description ends with one short credit line (`youtube.credit`, default `🎥 Original: <channel>`).
