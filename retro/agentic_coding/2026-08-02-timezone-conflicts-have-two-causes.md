# Two causes, one symptom — and a determinism bug hiding under it

**Cycle:** T12 correction, prompted by the traveller importing the rest of a 1193-item trip.
1549 tests pass.

## What happened

The import log carried 14 warnings of the form *"EXIF offset −420 min disagrees with its GPS zone
Europe/Vienna"*. The traveller asked whether anything needed worrying about. The stage was doing
exactly what it was designed to do, and it was wrong six times.

The rule since P01 was: on disagreement, **trust the tag for the instant and GPS for the zone**.
That was derived from a real case — a phone still set to Pacific time wrote `08:26 -07:00` in
Vienna, and reading `08:26` as Vienna local put it nine hours from frames shot seconds apart.

The rule is right for that case. But the same symptom has a **second cause**, and the rule is
exactly wrong for it: the clock was *already* on local time and only the offset tag was stale. Then
the wall reading is correct, and applying the tag throws the photo nine hours forward.

Both were in one trip. Checking each conflicted frame against its own camera's neighbouring frames:

| | |
| --- | --- |
| Shift was correct | **8** |
| Shift was wrong | **6** |
| Undetermined | 1 |

IMG_1993 was the clearest: neighbours at 15:44 and 16:13, raw reading 15:59 — already right. It
had been pushed to **00:59 the next day**.

## The lesson

**When one symptom has two causes, a fixed rule corrupts one group to serve the other.** There is
no cleverer version of "always trust the tag" that gets both right, because the tag is the same in
both cases. The information needed is elsewhere.

And it was already in the data: *which reading lands next to the photographs taken around it.* The
camera's own clock is no help — it is the thing in doubt, and it is not even monotonic when a device
flips between two zone settings mid-trip. But a photo belongs near its neighbours, so compute both
instants and keep the one closer to a same-device frame whose tag and GPS already agreed.

With no neighbour inside six hours there is no evidence, and the documented default stands —
announced as *"no nearby photo from this camera to check against"* rather than presented as settled.

## The worse bug underneath

Fixing this changed 143 GoPro timestamps by an hour, which I had not asked for. Tracing it:

- `timezones` runs **before** `gps_backfill`.
- Build 1: a GoPro clip has no coordinates → resolved from a same-device neighbour.
- Build 2: `gps_backfill` has since interpolated coordinates → resolved as GPS-backed, an hour
  adrift.

**The same source tree resolved to different timestamps on a re-run.** This predated my change and
is worse than the bug I set out to fix, because it is silent and it moves data every time. It is
also circular: the interpolation is computed *from* the timestamps this stage produces.

`media.has_gps` only means "lat and lon are set". After a backfill that is true of items whose
position was inferred from other items. The fix is to ask not *"is this field set?"* but *"who set
it, and had they run the first time?"* — `gps_source` already distinguishes a measurement from a
derivation, and only the measurement is this item's own evidence.

**This is the "run it twice" family again**, and the fifth instance in the project. The others were
staleness (an aggregate not re-deriving); this one is the mirror image — a stage seeing *more* than
it did the first time.

## What the verification looked like

The diagnosis and the proof used the same test, which is the useful part: for every conflicted
frame, does the resolved time fall between the two nearest confidently-resolved frames from the
same camera?

| | before | after |
| --- | --- | --- |
| In sequence | 8 | **42** |
| Out of sequence | **6** | **0** |
| Undetermined | 1 | 2 |

The two undetermined are the first and last frame of the trip, which have no neighbour on one side.

**The new unit tests were shown to fail against the old behaviour** — 5 of 9, with the failure
message printing the exact defect (`Reading 2026-07-19T15:59 as 2026-07-20T00:59 local`). Forcing
`NEIGHBOUR_WINDOW = 0` reproduces the old rule, which made that check two lines.

## A near miss worth recording

My first version of `_has_measured_gps` required a *positive* marker (`gps_source in (EXIF,
MANUAL)`). Eight existing tests failed, because the `make_media` fixture sets `lat`/`lon` without
setting `gps_source`. The tempting move was to edit eight tests to fit my code. Instead I checked
production — real rows do carry `gps_source = 'exif'` — and then narrowed the rule to exclude
`INTERPOLATED` specifically. That is the more conservative change: an item of unknown provenance
keeps whatever behaviour it had, and no test needed rewriting to accommodate me.

**Editing many existing tests to make a new one pass is a signal to re-derive the change, not a
chore to work through.**
