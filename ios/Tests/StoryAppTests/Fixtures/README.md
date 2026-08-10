# StoryAppTests fixtures

## `bundle/` — a real report, and everything it points at

Produced by the pipeline, not by hand:

```bash
uv run story-book build tests/fixtures/media --out /tmp/report-fixture --no-cloud
```

`bundle/` is then the **reference closure** of `/tmp/report-fixture/report`: every page, plus every
local file any page's `src` or `href` resolves to, copied at its path relative to the output
directory. Regenerating it means rebuilding and re-taking that closure — not copying three
directories that look right.

That distinction is the point. The four roots a report actually reaches into are:

| Root | Holds |
| --- | --- |
| `report/` | `index.html`, `days/<date>.html`, `style.css`, `vendor/leaflet.{js,css}` |
| `thumbs/` | grid thumbnails |
| `previews/` | the full-size image behind each tap-through |
| `.cache/video/` | **video poster frames** |

The last one is the trap. `.cache/` is named for something disposable and starts with a dot, so a
bundle assembled from the three obvious directories loses every video poster and nothing fails
loudly — the day page renders with a blank cell where a clip should be. `ReportBundleTests`
asserts the reference exists so that a change to where posters live breaks here rather than in a
reader's hands.

It also means the directory handed to `loadFileURL(_:allowingReadAccessTo:)` is the one *containing*
`report/`. Granting access to `report/` alone renders every page perfectly with every image broken.

## Size

~488 KB, most of it vendored Leaflet, which has to be real: the offline map test asserts that
Leaflet loaded from disk and drew the day's route with every network load blocked.
