import Foundation
import Testing

@testable import StoryService

/// The loop, driven against a **real running service**: create a trip, declare a folder of exported
/// media, upload the bytes to a real presigned `PUT`, negotiate again, prepare, build, and follow
/// the build to a terminal state.
///
/// Skipped unless `STORY_SERVICE_LIVE_URL` is set, because the committed suite must not need a host
/// — and must never need an S3 bucket, which does not exist yet. This is the one test that proves
/// the wire contract against the code that implements it rather than against a fake of it; run it
/// by hand, and read what it prints.
///
/// ```bash
/// # one terminal: a local S3 (moto verifies no signature, so it proves the round trip, not S3)
/// cd service && uv run moto_server -p 5000
/// # another: the service. `uv run`, never ./.venv/bin/uvicorn -- that leaves story-book off PATH
/// cd service && STORY_SERVICE_S3_BUCKET=story-media STORY_SERVICE_S3_REGION=eu-central-1 \
///   STORY_SERVICE_S3_ENDPOINT_URL=http://127.0.0.1:5000 AWS_ACCESS_KEY_ID=local \
///   AWS_SECRET_ACCESS_KEY=local uv run uvicorn storybook_service.app:app --port 8000
/// # then
/// cd ios && STORY_SERVICE_LIVE_URL=http://127.0.0.1:8000 \
///   STORY_SERVICE_LIVE_MEDIA=$PWD/../tests/fixtures/media swift test --filter LiveLoop
/// ```
struct LiveLoopTests {
    static var liveURL: URL? {
        ProcessInfo.processInfo.environment["STORY_SERVICE_LIVE_URL"].flatMap(URL.init(string:))
    }
    static var mediaDirectory: URL? {
        ProcessInfo.processInfo.environment["STORY_SERVICE_LIVE_MEDIA"].map {
            URL(filePath: $0)
        }
    }
    static var isConfigured: Bool { liveURL != nil && mediaDirectory != nil }

    @Test(.enabled(if: LiveLoopTests.isConfigured))
    func aPhotographBecomesABuild() async throws {
        let endpoint = ServiceEndpoint(baseURL: Self.liveURL!)
        // A fresh identity per run, and it is not tidiness. Asset keys are **per user** (question 4),
        // so a second run under one identity finds every object already stored and its "first"
        // negotiate needs nothing — which is correct behaviour and makes the first-upload control
        // impossible to state. Measured, not guessed: the run that discovered this had a brand-new
        // trip whose first negotiate answered `needed: 0`.
        let identity = UnverifiedIdentityHeader(
            identity: "loop-\(UUID().uuidString.prefix(8).lowercased())@example.com")
        // A real URLSession over the loopback. `.default`, not the background configuration: a test
        // process is not an app and has nothing to be relaunched into.
        let session = URLSession(configuration: .default)
        let client = StoryServiceClient(endpoint: endpoint, identity: identity, session: session)

        // Copy the media somewhere writable so this never touches the fixture tree.
        let staging = FileManager.default.temporaryDirectory.appending(
            path: "live-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: staging, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: staging) }
        for name in try FileManager.default.contentsOfDirectory(atPath: Self.mediaDirectory!.path)
        where !name.hasPrefix(".") {
            try? FileManager.default.copyItem(
                at: Self.mediaDirectory!.appending(path: name),
                to: staging.appending(path: name))
        }

        let scan = try ExportFolder.scan(in: staging)
        let declarations = scan.declarations
        #expect(!declarations.isEmpty)
        print("[loop] declaring \(declarations.count) assets")
        for (hash, names) in scan.undeclaredDuplicates {
            // Byte-identical copies. Printed because the service can store one filename per hash,
            // so these names will not appear in `trip.json` — see cross-task request XT-3.
            print("[loop] byte-identical duplicate not declared: \(names) under \(hash.prefix(12))…")
        }

        let trip = try await client.negotiate.createTrip(name: "Live loop")
        print("[loop] trip \(trip.id)")

        let first = try await client.negotiate.negotiate(tripID: trip.id, assets: declarations)
        print("[loop] first negotiate: needed \(first.needed.count), bytes \(first.bytesToTransfer)")
        #expect(first.needed.count == declarations.count)
        #expect(first.bytesToTransfer > 0)
        #expect(first.multipart == false)

        let transport = URLSessionUploadTransport(configuration: .default)
        let queue = try UploadQueue(
            stateURL: staging.appending(path: "upload_queue.json"), transport: transport,
            retryDelay: .milliseconds(200))
        await queue.enqueue(first.needed, filesIn: staging)
        let report = await queue.run()
        print(
            "[loop] uploaded \(report.uploaded.count), bytes \(report.bytesTransferred.map(String.init) ?? "unmeasured"), failed \(report.failed)"
        )
        #expect(report.failed.isEmpty)
        #expect(report.needsRenegotiation.isEmpty)
        // Against a real URLSession the byte count is a measurement, unlike under a URLProtocol
        // stub — so here it must be present and must equal what was declared.
        #expect(report.bytesTransferred == first.bytesToTransfer)

        // The criterion, against the real service: nothing left to send.
        let second = try await client.negotiate.negotiate(tripID: trip.id, assets: declarations)
        print("[loop] second negotiate: needed \(second.needed.count), have \(second.have.count)")
        #expect(second.needed.isEmpty)
        #expect(second.bytesToTransfer == 0)

        // And the same photographs declared under a *different trip of the same user* need nothing
        // either: the store is keyed per user by content hash, so cross-trip dedup is real. Printed
        // as well as asserted, because it is the strongest evidence that hashing — not bookkeeping —
        // is what makes the second upload free.
        let sameUserSecondTrip = try await client.negotiate.createTrip(name: "Live loop, again")
        let acrossTrips = try await client.negotiate.negotiate(
            tripID: sameUserSecondTrip.id, assets: declarations)
        print("[loop] a second trip for the same user: needed \(acrossTrips.needed.count)")
        #expect(acrossTrips.needed.isEmpty)

        let prepared = try await client.negotiate.prepareSource(tripID: trip.id)
        print("[loop] prepare: fetched \(prepared.fetched), missing \(prepared.missing.count)")
        #expect(prepared.complete)
        #expect(prepared.scaffolded)

        let start = try await client.jobs.startBuild(tripID: trip.id)
        print("[loop] job \(start.job.jobID) created=\(start.created)")

        var readings = 0
        var last: JobStatus?
        // A fixture-sized build finishes in seconds, so two-second polling sees three readings and
        // "monotonically advancing" is barely observable. `STORY_SERVICE_LIVE_POLL_MS=200` is how
        // the advance was actually watched.
        let pollMilliseconds =
            ProcessInfo.processInfo.environment["STORY_SERVICE_LIVE_POLL_MS"].flatMap(Int.init) ?? 2000
        for try await reading in client.jobs.follow(
            jobID: start.job.jobID, every: .milliseconds(pollMilliseconds))
        {
            readings += 1
            last = reading.status
            print(
                "[loop] \(reading.status.stateRaw) stage=\(reading.status.stage ?? "-") "
                    + "\(reading.status.overallPosition) inside \(reading.status.stageCount)"
                    + (reading.wentBackwards ? "  <-- WENT BACKWARDS" : ""))
            #expect(reading.wentBackwards == false)
        }
        print("[loop] finished after \(readings) readings; degraded=\(last?.degraded.map(String.init) ?? "unmeasured")")
        for line in last?.degradedSummary ?? [] { print("[loop] degraded: \(line)") }
        #expect(last?.state == .succeeded)
        #expect(last?.tripJSONPath != nil)
    }
}
