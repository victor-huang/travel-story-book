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

## Why soft tracks rather than burned-in text

- A viewer can **turn them off**, or pick between languages. Burned-in text offers neither.
- Adding or fixing a language is a **re-mux, not a re-render** — a second or two, no re-encode.
- **No font problem.** The player draws the text with its own fonts. Burning Chinese into the
  frames would need a CJK font present on the machine *and* an ffmpeg built with libass, and
  neither can be assumed — the ffmpeg this project was developed against has no `subtitles`
  filter at all.

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
