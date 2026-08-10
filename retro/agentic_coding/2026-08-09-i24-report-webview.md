# I24 — the report in a webview

Wave 2 (M1), iOS. One task delivered, four found blocked. Every entry below is a test that was
wrong before any product code was.

---

### A test helper let the navigation delegate die, and five of seven tests still passed

**Cycle:** Wave 2 / I24
**Cost:** ~25 min, three debug cycles
**Category:** wrong-test

**Symptom.** `dayNavigationLoadsTheNextDay` and `theCrumbReturnsToTheIndex` failed with the webview
still on the page it started on, after a three-second wait. The same click worked in a standalone
probe. Everything else in the suite passed: images decoded, the poster decoded, the map drew, the
external link was intercepted.

**Root cause.** A helper built a `WKWebView`, created a local `ReportLoader`, called
`attach(to:)`, and returned only the webview. `WKWebView.navigationDelegate` is a **weak**
reference, so the loader deallocated at the return and the delegate went nil. Every link in the
report went dead while everything that does not involve a navigation kept working — which is
exactly why the failure looked like a timing bug for two rounds of "wait longer".

**Fix.** Not in the test: `ReportLoader` now **owns** its `WKWebView` as a stored property, so
there is no order of operations left to get wrong and no lifetime to document. A regression test
asserts `loader.webView.navigationDelegate === loader`.

**Lesson.** A delegate is a lifetime contract, and a weak one fails silently and *partially*. When
a partial failure is possible, the surprise is which tests still pass — those are the ones that
tell you the object is half-alive.

---

### The obvious control passed alone and failed alongside its neighbours

**Cycle:** Wave 2 / I24
**Cost:** ~20 min
**Category:** wrong-test

**Symptom.** The criterion for I24 is that the report's images actually appear, asserted as
`img.naturalWidth > 0`. Its control was the natural one: load the page granting read access to
`report/` only — the narrow root that every image reference climbs out of — and require every
image to starve. It reported 14, then 15, images decoded. Run with `--filter` on its own, it
passed.

**Root cause.** Every `WKWebView` in a process shares one WebContent process, its resource cache
and its file-access grants. Another test had already loaded the same media with the correct
read-access root, so the narrow grant was no longer observable. Marking the suite `.serialized`
did **not** fix it: the leak is over the process lifetime, not the parallelism. Giving each test
its own copy of the bundle, at unique paths, did not fix it either.

**Fix.** Replaced with a control that cannot be satisfied by another test's side effect: delete one
referenced thumbnail and require that exactly the `<img>`s pointing at it fail to decode, and no
others. It also had to be counted by source rather than by element — one thumbnail appears twice
on a day page, as a highlight and in the timeline, so one deleted file breaks two elements.

**Lesson.** A control that only holds when it runs first is not a control. Ask what *else* could
satisfy the assertion — here, another test — before trusting it.

---

### `/var` is a symlink, and Foundation crosses it only for files that exist

**Cycle:** Wave 2 / I24
**Cost:** ~15 min
**Category:** wrong-assumption

**Symptom.** After deliberately deleting a thumbnail, the bundle's reference check flagged the right
file with the **wrong reason**: `outsideBundle` instead of `missing`. Printing the paths showed the
bundle root as `/var/folders/…` and the deleted file as `/private/var/folders/…` — from the same
directory, through the same normalization.

**Root cause.** `standardizedFileURL` and `resolvingSymlinksInPath` both rewrite across the
`/var` → `/private/var` symlink for a path that exists and leave it alone for one that does not.
So a containment test built on path prefixes gives different answers for a file depending on
whether it is there — which is precisely the case the check exists to handle. Composing the two
functions in either order did not produce a stable spelling.

**Fix.** Containment for the reference check is now **arithmetic on path components** — resolve
`../../thumbs/x.jpg` against `["report", "days"]` to `["thumbs", "x.jpg"]`, with `nil` for anything
that climbs out — and touches the filesystem only to ask whether the resulting file exists. The
helper is tested directly, not only through its callers.

**Lesson.** Two from this repo, both re-earned: *a geometry or measurement helper is as likely to
be the bug as the code*, and a wrong answer that names the right file is the kind that survives
review. The only reason this was caught is that the deliberate-failure control existed at all.

---

### Things that were true and worth keeping

- **WKWebView runs hostless under `swift test`**, including `evaluateJavaScript`, content rule
  lists and snapshots. Unlike PhotoKit (D10/D11, which needs a bundle identity and a human click),
  the whole book path has real CI coverage.
- **Offline can be a property of the test.** A `WKContentRuleList` blocking `^https?://` makes
  every rendering test run with the network off regardless of the machine, and the map test's
  control asserts that no tile decoded — the previous generation of "works offline" tests in this
  repo asserted nothing true.
- **Looking at the render found what no assertion did.** A snapshot of the day page confirmed in
  seconds that Leaflet had initialised, the stylesheet applied and the fixture art decoded — and
  that the blank map background is the documented offline fallback rather than a failure.
- **A rendered report reaches into four directories**, one of them `.cache/video/` for video
  posters. Recorded as open question 12: what the service ships has to be the report's reference
  closure, not a list of directories that look right.
