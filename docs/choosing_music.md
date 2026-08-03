# Choosing music for the reel

`story-book reel --music <file>` takes any audio file ffmpeg can read. **Nothing ships with the
tool** — no audio can be redistributed without a licence — so every reel starts with you finding
a track. This is how to find one that fits, and why the usual "pick something you like" advice is
not quite enough here.

The reel has three mechanics that change what a *good* track is:

1. **Clip audio plays, and the music ducks 8.4 dB underneath it.** The music is a bed, not the
   lead.
2. **Short tracks loop, long tracks get cut off** with a tail fade.
3. **Cuts are not beat-aligned yet** (T51), so tempo matters for a different reason than you'd
   expect.

Each of those turns into a concrete thing to check before downloading.

---

## Step 1 — read your own reel's numbers first

Don't choose in the abstract. Build the reel once with no music, then look at what you actually
have:

```bash
story-book reel --out <dir>
python3 -c "
import json,sys; d=json.load(open('<dir>/reel/reel.json'))
tot=d['video']['duration_seconds']
clip=sum(v['seconds'] for v in d['excerpts']['by_asset'].values())
print(f'reel      {tot:6.1f}s')
print(f'clip audio{clip:6.1f}s  ({100*clip/tot:.0f}% of the reel)')
print(f'music bed {tot-clip:6.1f}s  ({100*(1-clip/tot):.0f}%)')
print('clips with sound:', len(d['audio']['clips_with_sound']))
"
```

Two numbers come out of this and both change the decision:

- **Total duration** — the length to look for.
- **Clip-audio share** — how much of the reel the music is *not* the main thing. Below ~10%, the
  track carries the whole film and is worth agonising over. Above ~30%, it is background for a
  third of the runtime and a busy track will fight the footage.

---

## Step 2 — the four properties that actually matter

### Length: aim slightly longer than the reel

A track shorter than the reel loops, and a loop point is audible unless the track was written as
one. A track longer than the reel gets trimmed with a `music_fade_seconds` fade, which always
sounds deliberate.

**So: find something 10–30 seconds longer than your reel and stop worrying.** One good 3-minute
track beats a perfect 90-second one played twice.

### Tempo: steady beats interesting

This is the counter-intuitive one. Cuts are currently on a fixed cadence
(`seconds_per_still`, default 3.0 s), *not* aligned to the music. A track with a rock-solid tempo
will appear to line up with the cuts by coincidence a good fraction of the time. A track that
drifts — rubato piano, ambient with no pulse, live recordings with human timing — never lines up,
and the mismatch reads as sloppiness rather than as style.

It matters twice over: when beat-synced cutting lands (T51), onset detection works far better on
a constant tempo. **Prefer catalogues that publish BPM**, and prefer a single tempo throughout
over a track with a tempo change.

Useful range for a montage: **100–130 BPM**. Slower feels like a memorial; faster outruns 3-second
stills.

### Dynamic range: flat is a feature

Because the music drops 8.4 dB whenever a clip plays, a track built around a big quiet-to-loud
arc will get its climax swallowed if a clip happens to land on it. Worse, a track with long near
-silent passages will duck to nearly nothing and leave a hole.

**Want:** consistent mid-density throughout — a groove, a steady arrangement, a repeating figure.
**Avoid:** long ambient intros, dramatic drops, solo passages with lots of space, anything
described as "cinematic build".

### Mood: match the story, not the destination

The temptation is to match the *place* — accordion for Paris, mandolin for Italy. Resist it; it
reads as parody. Match what the trip **was**.

`<out>/story/story.json` is the best guide you have, because a model already read the whole trip
and wrote down what it was about. Look at `title`, `subtitle`, `narrative_voice` and the day
titles:

```bash
python3 -c "
import json; s=json.load(open('<dir>/story/story.json'))
print(s.get('title')); print(s.get('subtitle'))
print('voice:', s.get('narrative_voice'))
for d in s.get('days',[]): print(' ', d['date'], '-', d.get('title'))
"
```

A trip whose story is about art galleries and a concert wants different music from one about
hiking, even if both happened in Austria.

---

## Step 3 — where to look

Preferring sources that publish a **licence** and, ideally, **BPM**.

| Source | Licence | Notes |
| --- | --- | --- |
| **incompetech.com** (Kevin MacLeod) | CC BY 4.0 | Publishes BPM, length, instruments and a "feel" field. Written for video. The best fit for this workflow, and what the worked example below uses. |
| **Free Music Archive** | mixed CC | Check each track: CC BY and CC0 are fine, **CC BY-NC-ND is not** — "no derivatives" is the wrong licence for a soundtrack. |
| **archive.org** | mixed | Enormous, and mostly NC-ND when you filter for music. Usable but needs care. |
| **ccMixter** | mixed CC | Same caution. |
| **Uppbeat / Pixabay Music** | own licences | Free tiers exist; read the terms, some require an account or credit. |
| **Epidemic Sound / Artlist / Musicbed** | subscription | Paid, and the licensing question disappears. Best quality per minute of effort. |

### Licences to avoid

- **ND (no derivatives)** — synchronising to video is plausibly a derivative. Not worth the
  argument.
- **NC (non-commercial)** — fine for a family reel, a problem the moment there are ads.
- **"Free download" with no stated licence** — the absence of a licence is not permission.

### CC BY means attribution is required

Not optional, not "nice to have". Keep the credit line with the file so it does not get lost:

```
"Crinoline Dreams" Kevin MacLeod (incompetech.com)
Licensed under Creative Commons: By Attribution 4.0
http://creativecommons.org/licenses/by/4.0/
```

Store the audio and its attribution **outside** `--out` and outside the repository. `--out` is
disposable, and no audio should ever be committed.

---

## Step 4 — search vocabulary that works

Catalogue search boxes respond better to *feel* words than to genre. Terms that tend to land well
for a travel montage:

- **Good:** bright, uplifting, warm, grooving, relaxed, bouncy, light, acoustic, optimistic
- **Bad for this purpose:** cinematic, epic, trailer, build, dramatic, dark, eerie, tense,
  comedic, quirky

"Epic" and "trailer" are the two most common mistakes. Both describe music with a huge dynamic
arc, which is exactly what ducking ruins.

If the catalogue exposes structured fields (incompetech's `pieces.json` does), filter on them
instead of guessing: `feel` containing `Uplifting`/`Bright`, `bpm` between 100 and 130, length
above your reel duration, and exclude `Dark`/`Eerie`/`Humorous`/`Action`.

---

## Step 5 — audition against the clips, not the opening

Every track sounds fine over the first few photographs. What separates them is behaviour at the
ducking boundaries.

Render candidates to separate files so you can compare — the reel always writes to
`<out>/reel/trip.mp4`, so four runs leave you with only the fourth:

```bash
for name in "Track A" "Track B"; do
  story-book reel --out <dir> --music ~/Music/story-book/"$name.mp3"
  cp <dir>/reel/trip.mp4 "<dir>/reel/variants/trip - $name.mp4"
done
```

Each candidate is only a re-mix, not a re-render: segments are cached, so it costs about 30
seconds rather than the full render.

Then get the timestamps to skip to, and listen *there*:

```bash
python3 -c "
import json; d=json.load(open('<dir>/reel/reel.json'))
for v in sorted(d['excerpts']['by_asset'].values(), key=lambda x: x['timeline_start_seconds']):
    t=v['timeline_start_seconds']
    print(f\"{int(t//60)}:{t%60:04.1f}  {v['filename']:20} {v['seconds']:.1f}s\")
"
```

Listen for: does the duck feel like a decision or a dropout? Does the music come back cleanly
after the clip? If several clips sit back to back, does the long ducked stretch sag?

---

## Worked example — this project's own trip

The reel: **160.5 s, 8 clips, 40.0 s of clip audio = 25% of the runtime.** The story's title is
*"Vienna in Art and Music, Then On to Munich"*; day titles cover museums, a concert at the
Musikverein, and Klimt.

Four CC BY candidates were shortlisted from incompetech by filtering `pieces.json` on feel, BPM
100–130, and length above 160 s, then rendered and compared:

| Track | BPM | Length | Character |
| --- | --- | --- | --- |
| Porch Swing Days – faster | 130 | 3:05 | acoustic guitars, folky |
| Inspired | 120 | 4:46 | bright, modern |
| On My Way | 126 | 4:13 | warm, flutes and clarinets |
| **Crinoline Dreams** | **120** | **4:06** | **piano and strings** |

**Chosen: Crinoline Dreams.** Piano and strings against a story about galleries and a concert,
rather than folk guitar which fought it. At 120 BPM it is the least dense of the four, which
matters when the music spends a quarter of the reel 8.4 dB down. And at 4:06 it comfortably
covers 160.5 s, so nothing loops.

The general shape of that decision is the reusable part: **shortlist on the measurable properties,
then choose on mood — and listen at the ducking boundaries before committing.**
