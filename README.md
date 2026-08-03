# Travel Story Book

Point it at one trip's folder of photos and videos. Get back an organized export, a
browsable offline HTML timeline and map, and a per-day package you can hand to ChatGPT to
write the travel journal.

```bash
uv sync
uv run story-book profile ~/Trips/Europe2026
uv run story-book build ~/Trips/Europe2026 --out ~/story-out
open ~/story-out/index.html
```

## Guarantees

- **Non-destructive.** The source folder is opened read-only and never modified, moved, or
  deleted. "Deduplication" selects a keeper; it never removes a file.
- **Resumable.** Every stage result is cached by file content hash. Interrupt a run and
  re-invoke it — only unfinished work is recomputed. There is no runtime target; overnight
  is fine.
- **Private.** No telemetry. Media near your configured home location is excluded from
  exports by default. `--no-cloud` completes the whole pipeline with zero network calls.

## Status

Early development. See [`dev_plan/`](./dev_plan) for the design
([plan](./dev_plan/mvp_process_from_picture_to_stories.md)) and current progress
([tracker](./dev_plan/implementation_tracker.md)). Development retros — errors hit and lessons
learned per cycle — live in [`retro/agentic_coding/`](./retro/agentic_coding).

## Guides

- [Choosing music for the reel](./docs/choosing_music.md) — no audio ships with the tool, so
  `story-book reel --music` always needs a track you supply. Where to find one that is licensed
  for the use, and which of its properties actually matter once clip audio and ducking are in play.
- [Subtitles on the reel](./docs/subtitles.md) — selectable subtitle tracks in another language,
  built from `translations` in `story.json`. Soft tracks, so no CJK font or libass build required.
- [Truth-set format](./docs/truth_set.md) — hand-labelling a trip so the eval harness can score
  events, duplicates and keeper choices.

## System dependencies

Python deps come from `uv sync`. Two stages also need system binaries:

```bash
brew install exiftool ffmpeg
```

`exiftool` is required for metadata extraction, `ffmpeg` for video analysis. Everything else
runs without them.

## License

MIT
