# Subtitles on the reel

The reel's title cards and captions are in whatever language `story.json` is written in. A
selectable subtitle track puts the same text in another language at the bottom of the picture,
without changing a single frame:

```bash
story-book reel --out <dir> --subtitles zh          # Mandarin
story-book reel --out <dir> --subtitles zh,en       # both, viewer picks
```

You get a `.vtt` beside the video **and** a `mov_text` track muxed inside it, so the language
menu works in a normal player and the sidecar also works on its own:

```
reel/
  trip.mp4        video, audio, and one subtitle stream per language
  trip.zh.vtt     the same cues, usable standalone
  trip.en.vtt
```

## Burned-in, for players that ignore subtitle tracks

```bash
story-book reel --out <dir> --subtitles zh --burn-in zh
```

That writes a **second** file, `trip.zh.mp4`, with the text drawn into the frames. The clean
`trip.mp4` is never overwritten — burn-in re-encodes and cannot be undone.

Use it when the player can't be trusted: a TV over USB, a social upload that strips subtitle
tracks, someone else's phone. Prefer the soft track everywhere else.

The text is drawn with **Pillow and composited by ffmpeg's `overlay`**, not by the `subtitles`
filter, which needs a build with libass — a stock Homebrew ffmpeg has none, so that route would
simply fail. The font is chosen per cue by *what has to be drawn*, so Chinese gets a CJK font and
English keeps the Latin one.

**If no font on the machine can draw the text, burn-in is declined** with a reason, rather than
drawing blanks and handing you a file that looks finished and says nothing. On Linux, install
Noto Sans CJK. The soft track needs no font at all and keeps working either way.

## Why soft tracks by default

- A viewer can **turn them off**, or pick between languages. Burned-in text offers neither.
- Adding or fixing a language is a **re-mux, not a re-render** — a second or two, no re-encode,
  no quality loss.
- **No font problem.** The player draws the text with its own fonts.

## Where the translations come from

`story.json`, in a `translations` block keyed by ISO 639-1 code. Three places carry it:

```json
{
  "language": "en",
  "translations": { "zh": { "title": "…", "subtitle": "…" } },
  "days": [
    { "date": "2026-07-18", "title": "Cathedrals and Palaces",
      "translations": { "zh": { "title": "大教堂与宫殿" } } }
  ],
  "captions": [
    { "asset_id": "5924958d", "caption": "Starting the day by tram.",
      "translations": { "zh": "乘电车开始这一天。" } }
  ]
}
```

`language` declares what the prose itself is (default `"en"`). Asking for that language gives you
the original text, which is why `--subtitles zh,en` produces a complete English track with no
translation work.

**The package asks the chat for this.** `prompt.md` requests a `translations` block when the
traveller wants another language, and `schema/story.schema.json` defines it — so the model that
already knows the trip does the translating, in context, rather than a separate service guessing at
proper nouns. If you translate by hand instead, edit `<out>/story/story.json` directly; it is
yours, and nothing in the pipeline rewrites it.

## What gets a cue

| Segment | Cue text |
| --- | --- |
| Trip title card | `title` and `subtitle`, two lines |
| Day title card | that day's `title` |
| Photograph | its entry in `captions`, if it has one |
| Video clip | its caption, if it has one |

A photograph with no caption gets no cue, so the subtitles are as sparse or as dense as the story
is. On a 61-segment reel with 23 captions that came to **28 cues**.

Cues are clamped so they never overlap. Segments overlap each other by `crossfade_seconds`, and
two overlapping VTT cues render *stacked* rather than replacing one another.

## A track never lies about its language

If `story.json` carries **no** translations for a language, **no track is written** — you get a
warning instead:

```
story.json carries no 'ja' translations, so no 'ja' track was written -- a track in one
language holding another's text would be a lie.
```

Partial translation is allowed. Untranslated cues show the original text, and both the console and
`reel.json` report how many:

```json
"subtitles": { "tracks": [
  { "language": "zh", "file": "trip.zh.vtt", "cues": 28,
    "translated_cues": 28, "fully_translated": true }
] }
```

Check that `translated_cues` matches `cues` before assuming a track is complete.

## Verifying one

The sidecar `.vtt` is plain text, so read it. To confirm the *embedded* track survived — `mov_text`
is where encodings tend to break — extract it back out and look:

```bash
ffprobe -v error -show_entries stream=index,codec_type:stream_tags=language \
  -of csv=p=0 <dir>/reel/trip.mp4
# 2,subtitle,zho
# 3,subtitle,eng

ffmpeg -v error -y -i <dir>/reel/trip.mp4 -map 0:s:0 -c:s webvtt /tmp/roundtrip.vtt
head /tmp/roundtrip.vtt
```

Cue count and characters should match the sidecar exactly.

For a **burned-in** file the only real check is the pixels — a filter that silently drew an empty
string produces a perfectly valid video. Compare the bottom of the frame against the clean reel at
a moment a cue is on screen, and at a moment none is:

```bash
python3 - <<'EOF'
import subprocess
def band(path, at, bottom):
    crop = "in_w:in_h/5:0:in_h*4/5" if bottom else "in_w:in_h/5:0:0"
    return subprocess.run(["ffmpeg","-v","error","-ss",str(at),"-i",path,"-vf",
        f"crop={crop},scale=64:16,format=gray","-frames:v","1","-f","rawvideo","-"],
        capture_output=True, check=True).stdout
def diff(a, b, at, bottom=True):
    x, y = band(a, at, bottom), band(b, at, bottom)
    return sum(abs(p-q) for p, q in zip(x, y)) / len(x)
clean, burned = "reel/trip.mp4", "reel/trip.zh.mp4"
for at in (1.0, 22.0):
    print(f"t={at}: bottom {diff(clean,burned,at):.2f}  top {diff(clean,burned,at,False):.2f}")
EOF
```

On the real trip that reads **6.24 bottom / 0.00 top** over a title card, and **0.11 / 0.14** during
a clip with no caption — text where a cue is, nothing where none is.
