# iOS client

Design lives in `dev_plan/ios_client_app.md`, `ios_client_implementation.md` and
`ios_backend_service.md`. Progress and task ownership live in
`dev_plan/ios_implementation_tracker.md` — **claim a task there before writing code.**

## Running the tests

Two suites, deliberately split, because they cost very different amounts.

### Hostless — fast, and what CI runs

```bash
cd ios && swift test          # StoryKit + StoryService, ~0.01 s
```

`PhotoExportTests` are **skipped** here. That is intentional: they need a `PHPhotoLibrary`, and
on the mac host that is the developer's own. An early version without the guard imported all
eleven fixtures into `~/Pictures/Photos Library.photoslibrary` and passed.

### App-hosted — the export path, simulator only

PhotoKit authorization goes through TCC, which attributes a request to a bundle identity. A
SwiftPM test target has none, so these tests need `StoryBookHost.xcodeproj` and its `HostApp`
(tracker D10).

```bash
cd ios
DEV=/Applications/Xcode.app/Contents/Developer
DEVICE=$(xcrun simctl list devices available | grep -m1 'iPhone 17 Pro' | grep -o '[0-9A-F-]\{36\}')

DEVELOPER_DIR=$DEV xcodebuild test \
  -project StoryBookHost.xcodeproj -scheme PhotoExportTests \
  -destination "platform=iOS Simulator,id=$DEVICE" \
  -test-timeouts-enabled YES -maximum-test-execution-time-allowance 120
```

**Always pass the timeout flags.** Without them a blocked permission dialog hangs the run
indefinitely and is indistinguishable from slow work.

#### The one manual step

The first run on a given simulator shows *"HostApp would like full access to your Photo
Library."* **Click "Allow Full Access" once.** The decision persists; later runs take about
5 seconds.

`xcrun simctl privacy <device> grant photos com.storybook.hostapp` looks like it should avoid
this and **does not work** on Xcode 26.3 / iOS 26 — `tccd` finds the grant and prompts anyway.
Open question 10 tracks automating the click via XCUITest.

If a run is ever killed while that dialog is on screen, the simulator is left wedged and every
later run inherits it. Recover with:

```bash
xcrun simctl shutdown "$DEVICE" && xcrun simctl erase "$DEVICE"
```

## Fixtures

- `tests/fixtures/media/` — 27 synthetic fixtures, rebuilt by `tests/fixtures/generate.py`.
- `tests/fixtures/device_media/` — real iPhone captures that **no generator can reproduce**, kept
  out of `media/` because `generate.py` starts by deleting that directory. See its README before
  adding or replacing anything there.
