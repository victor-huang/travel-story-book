# iOS backend service — ingest, build, and deliver a reel to a phone

> **Status: scheduled as Wave S, and code exists.** Created 2026-08-09 as a draft that nothing
> depended on; decomposed into S01–S07 in
> [`ios_implementation_tracker.md`](./ios_implementation_tracker.md) on 2026-08-09, and S01 landed
> the skeleton under `service/` on 2026-08-10. **Hosting, object storage, the queue substrate and
> the multi-trip index are still undecided** — see that tracker's open questions 13–18. This doc
> remains the source of truth for design; it is not yet a description of anything running.
>
> Companions: [`ios_client_app.md`](./ios_client_app.md) (the app half — export, curation,
> playback), [`reel_video_montage.md`](./reel_video_montage.md) (the reel itself) and
> [`reel_on_ios_avfoundation.md`](./reel_on_ios_avfoundation.md) (the eventual on-device
> renderer, which this service exists to defer).

The laptop workflow is unchanged. `story-book build` / `reel` against a local folder stays the
reference implementation, and this service runs **the same CLI, unmodified**, on a server.

---

## What the iOS app is

An **exporter**, not a pipeline. Its output contract is the source folder the CLI already
accepts: 1080px stills and 1080p clips with intact metadata — the same thing a Photos export
produces today, which is what the current trips were built from.

Everything downstream is untouched. That is the whole reason this is cheap.

| Layer | Responsibility |
| --- | --- |
| iOS app | Scope selection, 1080px export with metadata preserved, home filtering, upload, curation, playback |
| Service | Object storage, job queue, the existing CLI, delivery via CDN |
| CLI | Unchanged |

### Milestone 0 costs no backend at all

The app produces the folder and hands it over — AirDrop, Files, iCloud Drive — and the user runs
`story-book build` on their Mac exactly as now. That validates the hard part (metadata survival
through a downsized export) in isolation, and is shippable on its own for anyone with a laptop.
Everything below is Milestone 1 and later.

---

## Ingest: negotiate by hash, do not ship a zip

> **Split the upload, not the work.** T58 (2026-08-09) shipped a package organised one folder per
> day, told the user to open a chat per day, and got three one-day `story.json` files back — the
> day split was an upload-size workaround that silently became a division of the work. Ingest has
> exactly the same hazard: how the bytes are chunked must never decide what the pipeline treats
> as a unit. See `retro/agentic_coding/2026-08-09-the-package-asked-for-the-wrong-thing.md`.

Hash negotiation satisfies that by construction — the upload granularity is one asset, and the
unit of work stays the trip.

A zip is one ~600 MB blob for a typical trip (265 stills plus ~478 s of 1080p), and it fights
three properties the pipeline already has:

- **No resume.** A drop at 90% costs everything. Cellular and hotel wifi will do this routinely.
- **The server cannot start until the whole archive has landed** and been spooled.
- **Re-runs re-upload everything.** Adding ten photos to a trip costs another 600 MB.

Media is already keyed by BLAKE2b content hash, and re-importing the same photo is a no-op. So
negotiate first and upload only the gap:

```
POST /trips                          → {trip_id}
POST /trips/{id}/assets:negotiate    [{hash, filename, size}]  → {needed: [{hash, put_url}]}
PUT  <presigned object-store url>    per asset, parallel; multipart for clips
POST /trips/{id}/build               {config, overrides}        → {job_id}
GET  /jobs/{id}                      → {state, stage, done, total}
POST /trips/{id}/reel                {music_hash, aspect, day|from|to|place, name, subtitles}
                                                                → {job_id}
GET  /trips/{id}/reels/{reel_id}     → {playback_url, reel_json}
```

Consequences, all of them free rather than built:

- Per-file retry and natural resume.
- Deduplication across re-runs *and* across trips — the same photo in two trips uploads once.
- The API server never proxies 600 MB, because uploads go straight to object storage on
  presigned URLs.
- The music track is just another hash-addressed asset.

**`filename` must be preserved end to end.** `overrides.toml` addresses by filename, so
`IMG_1815.mov` staying `IMG_1815.mov` is what lets a user's corrections survive.

---

## Jobs

A build is minutes to hours; a reel render is minutes. Both are queued, neither is a request.

The resumability guarantee does the hard part already: every stage result is cached by content
hash and committed per item, so a killed worker **resumes** rather than restarts, and an
interrupt costs at most one item. Job progress is therefore real progress, not an estimate — it
can be read from `stage_result` rather than invented.

- **Push, don't poll.** APNs on completion. A spinner over a multi-minute render is the
  difference between a good app and a bad one.
- **One worker per trip at a time.** The DB is SQLite with a single-row `trip` table; concurrent
  builds against one trip are not a supported shape and should be serialised by the queue rather
  than defended against in code.

---

## Storage: what is durable and what is cache

The existing design already answers this and the service should not invent a second answer:
everything under `--out` is derived and disposable **except `<out>/story/`**, which holds what a
chat returned.

| Keep | Rebuildable |
| --- | --- |
| Uploaded assets | `previews/`, `thumbs/`, `package/`, `reel/` |
| `story.db` | `trip.json` |
| `<out>/story/` and overrides | Everything else |

### Retention — decided 2026-08-09, revisit 2027-08

**Everything is kept for one month after the most recent generation of a photo book or a reel.
The clock resets on every generation.** When it expires, the uploaded media is deleted; the
downsized renditions and all metadata are kept indefinitely.

Media is the entire storage bill, and this makes it flat per *dormant* trip while leaving an
active one fully re-cuttable. Iteration happens in the days after a trip, not a year later, so
the window covers the period when the media is actually wanted at full quality — and a user who
re-cuts on day 29 simply gets another month.

| Kept forever | Deleted one month after the last generation |
| --- | --- |
| `story.db`, `trip.json`, `<out>/story/`, `overrides.toml` | Uploaded video clips |
| `reel/` — video, `reel.json`, `.vtt` tracks | Uploaded stills *(see below)* |
| `previews/`, `thumbs/` | |
| Package 720p video proxies | |

**What resets the clock:** any successful `build`, `report`/photo book, `package`, or `reel` job
for that trip. The retained artifacts of a past generation do not — only a new one.

### The saving is almost entirely video

Worth being precise, because "keep the downsized assets" means two different things for the two
media kinds:

- **Stills gain nothing.** The uploaded still is already 1080px and `preview_long_edge` is 1600,
  and `Image.thumbnail()` never upscales — so the preview *is* the uploaded image, re-encoded.
  Keeping both is storing the same pixels twice. Keep the preview and drop the upload: the
  thumbnails stage exists for exactly this, *"so the outputs never need the originals"*
  (`pipeline/thumbnails.py:75`).
- **Video is where the bill lives.** A 720p CRF 28 proxy against a 1080p clip is roughly an order
  of magnitude smaller — on a Vienna-sized trip, tens of MB against ~480 MB.

Rough per-trip steady state: ~150 MB retained against ~600 MB uploaded, and the retained figure
is dominated by previews rather than footage.

### Inside the window everything is full quality; outside it, re-cuts are softer

Within the month, a re-cut at another aspect, with different music, or over a narrower
`--from/--to` reads the uploaded media and is indistinguishable from the first render. That is
the point of the window.

After expiry a re-cut reads proxies, and the quality cliff has a measurement: `01a9360` found a
proxy retains 41% of an original's detail at 1080p. Such a re-cut populates
`clips_enlarged_to_fit_frame`, and since `b0ec38b` the note distinguishes a small proxy from a
small original — so it says "there is nothing sharper to render from" rather than advising a fix
that no longer applies. Degraded and honest, not broken.

Worth surfacing in the app near expiry: *re-cut now at full quality, or re-upload later.* The
window is only useful if the user knows it is closing.

### The sweeper must not race a running job

Expiry is a scheduled sweep, and the obvious bug is deleting media out from under a queued or
in-flight render. The check is therefore **"expired **and** no job queued or running for this
trip"**, not the date alone — and a job starting must extend the deadline before it reads a
single file. Deletion is idempotent and logged; a trip whose media is already gone is not an
error.

---

## Delivery

**Progressive MP4 behind a CDN with short-lived signed URLs.** `AVPlayer` streams progressive
MP4 over HTTPS well, and the reel is 2–4 minutes at 1080p. HLS earns its complexity only for
adaptive bitrate or much longer videos — not in v1.

These are someone's family photographs. Never a public-read bucket; signed URLs should outlive a
viewing session, not a week.

### Known defect that blocks good playback — fix before any CDN work

`render_reel` muxes the final video with `-movflags +faststart` (`export/reel.py:1072`), which
puts the moov atom at the front so the file plays as it downloads. But `_write_subtitles` then
runs the `mov_text` mux (`export/subtitles.py:480`), which is `-c copy` **without**
`+faststart`, and finishes with `target.replace(video)`. The burn-in path
(`export/subtitles.py:445`) does set it; only the soft-track mux does not.

So whenever `--subtitles` is used, the shipped `trip.mp4` has its moov atom at the end. On a
laptop this is invisible — the player seeks. Streamed from a CDN, the player must fetch the
entire file before the first frame, which presents as a broken app.

One line to fix. It is worth recording *why* it survived: a schema or well-formedness test passes
on this file. Only a consumer can ask whether the moov atom is in the right place, and until now
every consumer was local.

---

## Privacy — the one place the hosted model is genuinely weaker

Guarantee 3 says media within `home_exclusion_km` of home is kept out of every export, and
`--no-cloud` completes the pipeline with zero network calls. A hosted build cannot honour the
second clause, and honours the first too late: `home_filter` runs before *export*, which is after
ingestion. Once bytes have left the phone, filtering server-side means the excluded photographs
were uploaded anyway.

**The filter must run client-side, before upload.** The coordinates are on the device, so this is
easy — it just has to be a decision rather than an oversight. Whatever is chosen, the plan doc's
guarantee gets amended to say what is actually true for the hosted path.

Also required, and unremarkable: per-user auth on every route, object-store paths that are not
guessable, and no cross-tenant reads.

---

## Non-goals

- Replacing or degrading the laptop workflow.
- Changing `trip.json`, the schemas, or anything in the frozen contracts. If the service needs a
  change there, it goes through the tracker's cross-task request table like anything else.
- Rendering the reel on device. That is
  [`reel_on_ios_avfoundation.md`](./reel_on_ios_avfoundation.md), and this service exists so it
  can be deferred.
- Multi-user sharing, comments, public links.

---

## Milestones

| # | Deliverable | Backend needed |
| --- | --- | --- |
| **M0** | App exports a folder; user builds on their Mac | None |
| **M1** | Hash negotiation, upload, queued `build`, `trip.json` back | Object store + queue + CLI |
| **M2** | Queued `reel`, signed CDN playback | + CDN, faststart fix |
| **M3** | APNs completion push, curation round trip via overrides | + push |
| **M4** | Story generation via API, `check-story` server-side | + key-holding proxy |

M1 should scaffold each trip with `story-book init --trip-dir <dir> [--like <other/config.toml>]`
rather than hand-writing config. `--like` is also the natural app affordance for "use my usual
settings on this new trip". One caveat, from the same day's other retro: a file that is safe to
read is not automatically safe to copy — the first `init` scaffolded `overrides.example.toml`,
whose worked example pins Europe filenames, and the build it told you to run died. If the service
scaffolds anything, it loads it in the new context and asserts it is empty.

M4 is small — `package.py` already emits the manifest, the published `story_schema.json`, and
`prompt.md`, and `check-story` already validates shape and grounding separately. The proxy exists
because an API key cannot ship in an app binary.

**M4's one hard constraint: all parts, one conversation, one `story.json`.** `--zip` now emits
`package.partNofM.zip` when the package exceeds `--max-part-mb` (default 200), splitting on day
boundaries. A service that loops over parts and calls the API once per part reproduces the exact
bug T58 just fixed — a per-day story that cannot even write a trip title, because day 1 does not
know where the trip ends. There is one `prompt.md` at the package root; every part goes to the
same conversation, and `check-story` runs once against the combined answer.

---

## Open questions

1. ~~**Retention.**~~ **Decided 2026-08-09:** keep everything for one month after the most recent
   photo book or reel generation, resetting on each; then delete the uploaded media and keep the
   downsized renditions, metadata and reel indefinitely. Revisit 2027-08. See *Retention* above.
   The remaining sub-question is whether a post-expiry re-cut from proxies is good enough to
   offer in the app at all, or whether it should prompt for a re-upload — answerable only by
   watching one.
2. **Home filter placement.** Client-side pre-upload (recommended), or an honest amendment to
   guarantee 3?
3. **Does the service store a source tree per trip, or per user?** Hash-addressed storage makes
   cross-trip dedup free, but complicates deletion — a shared asset cannot be removed with one
   trip.
4. **Config ownership.** Does the app expose thresholds, or does the service pin one config and
   keep the knobs on the laptop path?
